"""Incremental HTTP/1.1 request parser and connection state machine.

Pure in-memory: the caller feeds raw bytes (as they would arrive from a TCP
socket) via ``HttpConnection.feed()`` and gets back fully parsed requests.
No sockets, no external services.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Resource limits (defaults; all overridable per connection)
# ---------------------------------------------------------------------------
MAX_REQUEST_LINE_SIZE = 8 * 1024        # bytes, single request line
MAX_HEADER_LINE_SIZE = 8 * 1024         # bytes, single header line
MAX_HEADER_COUNT = 100                  # number of header fields
MAX_HEADERS_SIZE = 64 * 1024            # bytes, whole header block
MAX_BODY_SIZE = 1024 * 1024             # bytes, message body

CRLF = b"\r\n"
_TOKEN_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_VERSION_RE = re.compile(rb"^HTTP/(1\.[01])$")
_DIGITS_RE = re.compile(rb"^[0-9]+$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class HttpParseError(Exception):
    """Base class for all parse errors. ``status`` is the suggested HTTP
    response status code for this failure."""

    status = 400


class BadRequestLine(HttpParseError):
    """Malformed request line (bad method/target/version or bad syntax)."""


class RequestLineTooLong(BadRequestLine):
    """Request line exceeds the configured size limit."""


class BadHeader(HttpParseError):
    """Malformed header line (no colon, bad name, obs-fold, ctrl chars)."""


class HeaderTooLarge(HttpParseError):
    """Header block exceeds count/size limits (maps to 431)."""

    status = 431


class InvalidContentLength(HttpParseError):
    """Content-Length is missing digits, negative, or otherwise invalid."""


class ConflictingContentLength(InvalidContentLength):
    """Multiple Content-Length values that disagree with each other."""


class BodyTooLarge(HttpParseError):
    """Declared Content-Length exceeds the configured body limit (413)."""

    status = 413


class UnsupportedTransferEncoding(HttpParseError):
    """Transfer-Encoding is not supported by this parser (501)."""

    status = 501


class ConnectionClosedError(HttpParseError):
    """Data was fed to a connection that already saw ``Connection: close``."""

    def __init__(self, message: str, completed: Optional[List["HttpRequest"]] = None) -> None:
        super().__init__(message)
        #: Requests that were fully parsed before the offending extra data.
        self.completed: List[HttpRequest] = completed or []


# ---------------------------------------------------------------------------
# Parsed request
# ---------------------------------------------------------------------------
@dataclass
class HttpRequest:
    method: str
    target: str
    version: str                       # "1.0" or "1.1"
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    keep_alive: bool = True

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """First header value matching ``name`` (case-insensitive)."""
        lname = name.lower()
        for key, value in self.headers:
            if key.lower() == lname:
                return value
        return default


class _State(enum.Enum):
    REQUEST_LINE = enum.auto()
    HEADERS = enum.auto()
    BODY = enum.auto()
    CLOSED = enum.auto()


# ---------------------------------------------------------------------------
# Connection state machine
# ---------------------------------------------------------------------------
class HttpConnection:
    """Incremental parser bound to one logical connection.

    Feed arbitrary byte chunks with :meth:`feed`; it returns the list of
    requests completed by that chunk. Leftover bytes (half a header, part of
    a body, the start of a pipelined request) stay in an internal buffer and
    are combined with the next chunk.
    """

    def __init__(
        self,
        *,
        max_request_line_size: int = MAX_REQUEST_LINE_SIZE,
        max_header_line_size: int = MAX_HEADER_LINE_SIZE,
        max_header_count: int = MAX_HEADER_COUNT,
        max_headers_size: int = MAX_HEADERS_SIZE,
        max_body_size: int = MAX_BODY_SIZE,
    ) -> None:
        self.max_request_line_size = max_request_line_size
        self.max_header_line_size = max_header_line_size
        self.max_header_count = max_header_count
        self.max_headers_size = max_headers_size
        self.max_body_size = max_body_size

        self._buf = bytearray()
        self._state = _State.REQUEST_LINE
        self._method = b""
        self._target = b""
        self._version = b""
        self._headers: List[Tuple[str, str]] = []
        self._headers_size = 0
        self._body_length = 0

    # -- public API ---------------------------------------------------------
    @property
    def closed(self) -> bool:
        """True once the peer signalled ``Connection: close`` (or HTTP/1.0
        without keep-alive) and the final request was fully parsed."""
        return self._state is _State.CLOSED

    @property
    def buffered_bytes(self) -> int:
        """Bytes currently held in the reassembly buffer (for tests/observability)."""
        return len(self._buf)

    def feed(self, data: bytes) -> List[HttpRequest]:
        """Feed a chunk of bytes; return requests completed by it."""
        if self._state is _State.CLOSED:
            raise ConnectionClosedError("connection is closed; refusing more data")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("feed() expects bytes")
        self._buf += bytes(data)

        completed: List[HttpRequest] = []
        while self._state is not _State.CLOSED:
            request = self._advance()
            if request is None:
                break
            completed.append(request)
        if self._state is _State.CLOSED and self._buf:
            raise ConnectionClosedError(
                "received %d unexpected bytes after Connection: close" % len(self._buf),
                completed=completed,
            )
        return completed

    # -- state machine ------------------------------------------------------
    def _advance(self) -> Optional[HttpRequest]:
        if self._state is _State.REQUEST_LINE:
            if not self._parse_request_line():
                return None
            self._state = _State.HEADERS
            # fall through: headers may already be in the buffer

        if self._state is _State.HEADERS:
            if not self._parse_headers():
                return None
            self._body_length = self._determine_body_length()
            self._state = _State.BODY

        if self._state is _State.BODY:
            if len(self._buf) < self._body_length:
                return None
            body = bytes(self._buf[: self._body_length])
            del self._buf[: self._body_length]
            return self._finish_request(body)

        return None

    # -- request line -------------------------------------------------------
    def _parse_request_line(self) -> bool:
        line = self._take_line(self.max_request_line_size, RequestLineTooLong)
        if line is None:
            return False
        if not line:
            raise BadRequestLine("empty request line")

        parts = line.split(b" ")
        if len(parts) != 3 or any(part == b"" for part in parts):
            raise BadRequestLine("request line must be 'METHOD SP target SP HTTP/x.y'")
        method, target, version = parts

        if not _TOKEN_RE.match(method):
            raise BadRequestLine("invalid method: %r" % method)
        if not target or any(c < 0x21 or c == 0x7F for c in target):
            raise BadRequestLine("invalid request target: %r" % target)
        match = _VERSION_RE.match(version)
        if not match:
            raise BadRequestLine("unsupported HTTP version: %r" % version)

        self._method = method
        self._target = target
        self._version = match.group(1)
        self._headers = []
        self._headers_size = 0
        return True

    # -- headers ------------------------------------------------------------
    def _parse_headers(self) -> bool:
        while True:
            line = self._take_line(self.max_header_line_size, HeaderTooLarge)
            if line is None:
                return False
            if line == b"":
                return True  # empty line terminates the header block
            self._add_header(line)

    def _add_header(self, line: bytes) -> None:
        if line[:1] in (b" ", b"\t"):
            raise BadHeader("obsolete folded headers are not supported")
        name, sep, value = line.partition(b":")
        if not sep:
            raise BadHeader("header line without colon: %r" % line)
        if not _TOKEN_RE.match(name):
            raise BadHeader("invalid header name: %r" % name)
        if any((c < 0x20 and c != 0x09) or c == 0x7F for c in value):
            raise BadHeader("control character in header value: %r" % line)

        self._headers.append((name.decode("ascii"), value.strip(b" \t").decode("latin-1")))
        self._headers_size += len(line) + 2
        if len(self._headers) > self.max_header_count:
            raise HeaderTooLarge("too many headers (>%d)" % self.max_header_count)
        if self._headers_size > self.max_headers_size:
            raise HeaderTooLarge("header block too large (>%d bytes)" % self.max_headers_size)

    # -- body framing ---------------------------------------------------------
    def _determine_body_length(self) -> int:
        for name, _ in self._headers:
            if name.lower() == "transfer-encoding":
                raise UnsupportedTransferEncoding(
                    "Transfer-Encoding is not supported; use Content-Length"
                )

        values: List[bytes] = []
        for name, value in self._headers:
            if name.lower() == "content-length":
                values.extend(part.strip() for part in value.encode("latin-1").split(b","))
        if not values:
            return 0

        for value in values:
            if not value or not _DIGITS_RE.match(value):
                raise InvalidContentLength("invalid Content-Length: %r" % value)
        first = values[0].lstrip(b"0") or b"0"
        if any((v.lstrip(b"0") or b"0") != first for v in values):
            raise ConflictingContentLength("conflicting Content-Length values: %r" % values)

        length = int(first)
        if length > self.max_body_size:
            raise BodyTooLarge(
                "Content-Length %d exceeds limit of %d bytes" % (length, self.max_body_size)
            )
        return length

    # -- request completion ---------------------------------------------------
    def _finish_request(self, body: bytes) -> HttpRequest:
        connection_tokens = set()
        for name, value in self._headers:
            if name.lower() == "connection":
                connection_tokens.update(
                    token.strip().lower() for token in value.split(",")
                )

        if self._version == b"1.1":
            keep_alive = "close" not in connection_tokens
        else:  # HTTP/1.0
            keep_alive = "keep-alive" in connection_tokens

        request = HttpRequest(
            method=self._method.decode("ascii"),
            target=self._target.decode("latin-1"),
            version=self._version.decode("ascii"),
            headers=list(self._headers),
            body=body,
            keep_alive=keep_alive,
        )
        self._state = _State.REQUEST_LINE if keep_alive else _State.CLOSED
        return request

    # -- buffer helpers ---------------------------------------------------------
    def _take_line(self, limit: int, error_cls) -> Optional[bytes]:
        """Pop one CRLF-terminated line, or None if it hasn't fully arrived.

        Enforces the per-line size limit so a malicious peer cannot grow the
        buffer without bound while we wait for a line terminator.
        """
        end = self._buf.find(CRLF)
        if end == -1:
            if len(self._buf) > limit:
                raise error_cls("line exceeds %d bytes without CRLF" % limit)
            return None
        if end > limit:
            raise error_cls("line exceeds %d bytes" % limit)
        line = bytes(self._buf[:end])
        del self._buf[: end + 2]
        return line
