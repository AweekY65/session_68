"""Incremental HTTP/1.1 request parser and per-connection state machine.

Pure in-memory, stdlib-only. Feed raw bytes with ``feed()`` and get back
fully parsed ``HttpRequest`` objects. Handles TCP fragmentation (partial
request lines / headers / bodies) and HTTP pipelining on a keep-alive
connection.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Optional

CRLF = b"\r\n"
HEADER_END = b"\r\n\r\n"

# Token characters per RFC 7230 (for methods and header field names).
_TOKEN_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_VERSION_RE = re.compile(rb"^HTTP/(\d+)\.(\d+)$")
_CONTENT_LENGTH_RE = re.compile(rb"^[0-9]+$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HttpParseError(Exception):
    """Base class for all protocol/parse errors."""


class BadRequestLine(HttpParseError):
    """Malformed request line (bad shape, method, or HTTP version)."""


class RequestLineTooLong(HttpParseError):
    """Request line exceeded the configured limit."""


class HeaderTooLarge(HttpParseError):
    """Header block exceeded the configured byte limit."""


class TooManyHeaders(HttpParseError):
    """Header block contained more fields than allowed."""


class BadHeader(HttpParseError):
    """Malformed header field (bad name, obs-fold, missing colon, ...)."""


class InvalidContentLength(HttpParseError):
    """Content-Length value is not a non-negative decimal integer."""


class ConflictingContentLength(InvalidContentLength):
    """Multiple Content-Length values disagree."""


class BodyTooLarge(HttpParseError):
    """Declared Content-Length exceeds the configured body limit."""


class UnsupportedTransferEncoding(HttpParseError):
    """Transfer-Encoding other than identity is not supported."""


class ConnectionClosedError(HttpParseError):
    """Data was fed to a connection that is already closed."""


# ---------------------------------------------------------------------------
# Request object
# ---------------------------------------------------------------------------

@dataclass
class HttpRequest:
    method: str
    target: str
    version: str                      # e.g. "HTTP/1.1"
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> Optional[str]:
        """First value of a header (case-insensitive), or None."""
        lname = name.lower()
        for key, value in self.headers:
            if key.lower() == lname:
                return value
        return None

    def header_all(self, name: str) -> list[str]:
        lname = name.lower()
        return [v for k, v in self.headers if k.lower() == lname]

    @property
    def content_length(self) -> int:
        return len(self.body)

    @property
    def keep_alive(self) -> bool:
        """Whether the connection may stay open after this request."""
        tokens = {
            token.strip().lower()
            for value in self.header_all("Connection")
            for token in value.split(",")
        }
        if self.version == "HTTP/1.1":
            return "close" not in tokens
        # HTTP/1.0 and older: close unless explicitly kept alive.
        return "keep-alive" in tokens


# ---------------------------------------------------------------------------
# Connection state machine
# ---------------------------------------------------------------------------

class _State(enum.Enum):
    REQUEST_LINE = enum.auto()
    HEADERS = enum.auto()
    BODY = enum.auto()
    CLOSED = enum.auto()


class HttpConnection:
    """Incremental HTTP/1.1 server-side request parser for one connection.

    Usage::

        conn = HttpConnection()
        requests = conn.feed(b"GET / HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n")

    ``feed`` may be called with arbitrarily fragmented (or coalesced,
    pipelined) byte chunks. It returns the requests completed by this
    chunk; leftover bytes are buffered internally for the next call.
    """

    def __init__(
        self,
        *,
        max_request_line: int = 8192,
        max_header_bytes: int = 16384,
        max_headers: int = 100,
        max_body: int = 1024 * 1024,
    ) -> None:
        self.max_request_line = max_request_line
        self.max_header_bytes = max_header_bytes
        self.max_headers = max_headers
        self.max_body = max_body

        self._buf = bytearray()
        self._state = _State.REQUEST_LINE
        # Per-request scratch data.
        self._method = b""
        self._target = b""
        self._version = b""
        self._headers: list[tuple[str, str]] = []
        self._body_expected = 0

    # -- public API ---------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._state is _State.CLOSED

    def feed(self, data: bytes) -> list[HttpRequest]:
        """Feed raw bytes; return requests completed by this chunk."""
        if self._state is _State.CLOSED:
            raise ConnectionClosedError(
                "connection is closed; refusing further data"
            )
        self._buf += data
        completed: list[HttpRequest] = []
        while self._state not in (_State.CLOSED,):
            if self._state is _State.REQUEST_LINE:
                if not self._read_request_line():
                    break
            elif self._state is _State.HEADERS:
                if not self._read_headers():
                    break
            elif self._state is _State.BODY:
                if not self._read_body(completed):
                    break
            else:  # pragma: no cover - defensive
                break
        return completed

    # -- state handlers -----------------------------------------------------

    def _read_request_line(self) -> bool:
        idx = self._buf.find(CRLF)
        if idx < 0:
            if len(self._buf) > self.max_request_line:
                raise RequestLineTooLong(
                    f"request line exceeds {self.max_request_line} bytes"
                )
            return False
        if idx > self.max_request_line:
            raise RequestLineTooLong(
                f"request line exceeds {self.max_request_line} bytes"
            )
        line = bytes(self._buf[:idx])
        del self._buf[: idx + 2]
        self._parse_request_line(line)
        self._state = _State.HEADERS
        return True

    def _parse_request_line(self, line: bytes) -> None:
        parts = line.split(b" ")
        if len(parts) != 3 or any(part == b"" for part in parts):
            raise BadRequestLine(f"malformed request line: {line!r}")
        method, target, version = parts
        if not _TOKEN_RE.match(method):
            raise BadRequestLine(f"invalid method: {method!r}")
        match = _VERSION_RE.match(version)
        if not match:
            raise BadRequestLine(f"invalid HTTP version: {version!r}")
        major, minor = int(match.group(1)), int(match.group(2))
        if (major, minor) not in ((1, 0), (1, 1)):
            raise BadRequestLine(f"unsupported HTTP version: {version!r}")
        if not target:
            raise BadRequestLine("empty request target")
        self._method = method
        self._target = target
        self._version = version

    def _read_headers(self) -> bool:
        if self._buf[:2] == CRLF:
            # Empty header block: request line is directly followed by
            # the terminating blank line.
            del self._buf[:2]
            self._parse_headers(b"")
            self._state = _State.BODY
            return True
        if self._buf[:1] == b"\r":
            # Possible start of the blank line; wait for the next byte.
            return False
        idx = self._buf.find(HEADER_END)
        if idx < 0:
            if len(self._buf) > self.max_header_bytes:
                raise HeaderTooLarge(
                    f"header block exceeds {self.max_header_bytes} bytes"
                )
            return False
        if idx > self.max_header_bytes:
            raise HeaderTooLarge(
                f"header block exceeds {self.max_header_bytes} bytes"
            )
        block = bytes(self._buf[:idx])
        del self._buf[: idx + 4]
        self._parse_headers(block)
        self._state = _State.BODY
        return True

    def _parse_headers(self, block: bytes) -> None:
        self._headers = []
        if block:
            for line in block.split(CRLF):
                if line[:1] in (b" ", b"\t"):
                    raise BadHeader(f"obsolete folded header line: {line!r}")
                name, sep, value = line.partition(b":")
                if not sep:
                    raise BadHeader(f"header line without colon: {line!r}")
                if not _TOKEN_RE.match(name):
                    raise BadHeader(f"invalid header name: {name!r}")
                self._headers.append(
                    (name.decode("ascii"), value.strip(b" \t").decode("latin-1"))
                )
        if len(self._headers) > self.max_headers:
            raise TooManyHeaders(
                f"more than {self.max_headers} header fields"
            )
        self._body_expected = self._determine_body_length()

    def _determine_body_length(self) -> int:
        transfer_encodings = [
            v for k, v in self._headers if k.lower() == "transfer-encoding"
        ]
        if transfer_encodings:
            raise UnsupportedTransferEncoding(
                "Transfer-Encoding is not supported; "
                "only Content-Length bodies are accepted"
            )
        values: list[str] = []
        for key, value in self._headers:
            if key.lower() == "content-length":
                values.extend(part.strip() for part in value.split(","))
        if not values:
            return 0
        for value in values:
            if not _CONTENT_LENGTH_RE.match(value.encode("latin-1")):
                raise InvalidContentLength(
                    f"invalid Content-Length value: {value!r}"
                )
        lengths = {int(v) for v in values}
        if len(lengths) > 1:
            raise ConflictingContentLength(
                f"conflicting Content-Length values: {sorted(lengths)}"
            )
        length = lengths.pop()
        if length > self.max_body:
            raise BodyTooLarge(
                f"Content-Length {length} exceeds limit {self.max_body}"
            )
        return length

    def _read_body(self, completed: list[HttpRequest]) -> bool:
        if len(self._buf) < self._body_expected:
            return False
        body = bytes(self._buf[: self._body_expected])
        del self._buf[: self._body_expected]
        request = HttpRequest(
            method=self._method.decode("ascii"),
            target=self._target.decode("latin-1"),
            version=self._version.decode("ascii"),
            headers=self._headers,
            body=body,
        )
        completed.append(request)
        # Reset per-request scratch state.
        self._headers = []
        self._body_expected = 0
        if request.keep_alive:
            self._state = _State.REQUEST_LINE
        else:
            self._state = _State.CLOSED
        return True
