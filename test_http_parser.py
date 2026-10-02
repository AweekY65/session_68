"""Terminal-runnable tests for http_parser (no network, no browser)."""

import unittest

from http_parser import (
    BadHeader,
    BadRequestLine,
    BodyTooLarge,
    ConflictingContentLength,
    ConnectionClosedError,
    HeaderTooLarge,
    HttpConnection,
    InvalidContentLength,
    RequestLineTooLong,
    TooManyHeaders,
    UnsupportedTransferEncoding,
)

GET = b"GET /index.html HTTP/1.1\r\nHost: example.com\r\n\r\n"
POST = (
    b"POST /submit HTTP/1.1\r\n"
    b"Host: example.com\r\n"
    b"Content-Length: 5\r\n"
    b"\r\n"
    b"hello"
)


def feed_in_chunks(conn, data, size):
    """Feed data in fixed-size chunks, collecting completed requests."""
    out = []
    for i in range(0, len(data), size):
        out.extend(conn.feed(data[i : i + size]))
    return out


class TestCompleteRequests(unittest.TestCase):
    def test_simple_get(self):
        conn = HttpConnection()
        reqs = conn.feed(GET)
        self.assertEqual(len(reqs), 1)
        req = reqs[0]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.target, "/index.html")
        self.assertEqual(req.version, "HTTP/1.1")
        self.assertEqual(req.header("host"), "example.com")
        self.assertEqual(req.body, b"")
        self.assertFalse(conn.closed)

    def test_post_with_body(self):
        conn = HttpConnection()
        reqs = conn.feed(POST)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].method, "POST")
        self.assertEqual(reqs[0].body, b"hello")

    def test_methods_distinguished(self):
        for method in (b"GET", b"POST", b"PUT", b"DELETE", b"HEAD", b"PATCH"):
            conn = HttpConnection()
            reqs = conn.feed(method + b" /x HTTP/1.1\r\n\r\n")
            self.assertEqual(reqs[0].method, method.decode())

    def test_http10_request(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.0\r\n\r\n")
        self.assertEqual(reqs[0].version, "HTTP/1.0")
        self.assertFalse(reqs[0].keep_alive)
        self.assertTrue(conn.closed)


class TestFragmentation(unittest.TestCase):
    def test_byte_by_byte(self):
        conn = HttpConnection()
        reqs = feed_in_chunks(conn, POST, 1)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].body, b"hello")

    def test_split_inside_header_line(self):
        conn = HttpConnection()
        cut = GET.index(b"example") + 2  # middle of a header value
        self.assertEqual(conn.feed(GET[:cut]), [])
        reqs = conn.feed(GET[cut:])
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].header("Host"), "example.com")

    def test_split_crlf_boundary(self):
        conn = HttpConnection()
        cut = GET.index(b"\r\n") + 1  # between \r and \n
        self.assertEqual(conn.feed(GET[:cut]), [])
        self.assertEqual(len(conn.feed(GET[cut:])), 1)

    def test_partial_body(self):
        conn = HttpConnection()
        head, body = POST[:-5], POST[-5:]
        self.assertEqual(conn.feed(head + body[:2]), [])
        reqs = conn.feed(body[2:])
        self.assertEqual(reqs[0].body, b"hello")

    def test_random_chunk_sizes(self):
        for size in (2, 3, 7, 13):
            conn = HttpConnection()
            reqs = feed_in_chunks(conn, POST + GET, size)
            self.assertEqual([r.method for r in reqs], ["POST", "GET"])
            self.assertEqual(reqs[0].body, b"hello")


class TestPipelining(unittest.TestCase):
    def test_two_requests_one_feed(self):
        conn = HttpConnection()
        reqs = conn.feed(GET + POST)
        self.assertEqual([r.method for r in reqs], ["GET", "POST"])
        self.assertEqual(reqs[1].body, b"hello")

    def test_first_request_does_not_swallow_second(self):
        conn = HttpConnection()
        reqs = conn.feed(POST + GET)
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0].body, b"hello")
        self.assertEqual(reqs[1].target, "/index.html")

    def test_pipeline_split_across_feeds(self):
        conn = HttpConnection()
        data = GET + POST
        cut = len(GET) + 10  # inside second request's headers
        self.assertEqual(len(conn.feed(data[:cut])), 1)
        reqs = conn.feed(data[cut:])
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].method, "POST")

    def test_many_pipelined(self):
        conn = HttpConnection()
        reqs = conn.feed(GET * 5)
        self.assertEqual(len(reqs), 5)


class TestKeepAlive(unittest.TestCase):
    def test_http11_default_keep_alive(self):
        conn = HttpConnection()
        conn.feed(GET)
        self.assertFalse(conn.closed)
        reqs = conn.feed(GET)  # connection still usable
        self.assertEqual(len(reqs), 1)

    def test_connection_close(self):
        conn = HttpConnection()
        req = conn.feed(
            b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n"
        )[0]
        self.assertFalse(req.keep_alive)
        self.assertTrue(conn.closed)

    def test_http10_default_close(self):
        conn = HttpConnection()
        conn.feed(b"GET / HTTP/1.0\r\n\r\n")
        self.assertTrue(conn.closed)

    def test_http10_explicit_keep_alive(self):
        conn = HttpConnection()
        req = conn.feed(
            b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n"
        )[0]
        self.assertTrue(req.keep_alive)
        self.assertFalse(conn.closed)

    def test_feed_after_close_raises(self):
        conn = HttpConnection()
        conn.feed(b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n")
        with self.assertRaises(ConnectionClosedError):
            conn.feed(GET)

    def test_close_then_pipelined_data_not_processed(self):
        conn = HttpConnection()
        reqs = conn.feed(
            b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n" + GET
        )
        self.assertEqual(len(reqs), 1)  # second request must be ignored
        self.assertTrue(conn.closed)


class TestContentLengthErrors(unittest.TestCase):
    def _feed_cl(self, value: bytes):
        conn = HttpConnection()
        return conn.feed(
            b"POST / HTTP/1.1\r\nContent-Length: " + value + b"\r\n\r\n"
        )

    def test_non_numeric(self):
        with self.assertRaises(InvalidContentLength):
            self._feed_cl(b"abc")

    def test_negative(self):
        with self.assertRaises(InvalidContentLength):
            self._feed_cl(b"-1")

    def test_plus_sign(self):
        with self.assertRaises(InvalidContentLength):
            self._feed_cl(b"+5")

    def test_trailing_junk(self):
        with self.assertRaises(InvalidContentLength):
            self._feed_cl(b"12x")

    def test_conflicting_duplicates(self):
        conn = HttpConnection()
        with self.assertRaises(ConflictingContentLength):
            conn.feed(
                b"POST / HTTP/1.1\r\n"
                b"Content-Length: 5\r\n"
                b"Content-Length: 6\r\n"
                b"\r\n"
            )

    def test_conflicting_comma_list(self):
        with self.assertRaises(ConflictingContentLength):
            self._feed_cl(b"5, 6")

    def test_identical_duplicates_ok(self):
        conn = HttpConnection()
        reqs = conn.feed(
            b"POST / HTTP/1.1\r\n"
            b"Content-Length: 5\r\n"
            b"Content-Length: 5\r\n"
            b"\r\n"
            b"hello"
        )
        self.assertEqual(reqs[0].body, b"hello")

    def test_transfer_encoding_rejected(self):
        conn = HttpConnection()
        with self.assertRaises(UnsupportedTransferEncoding):
            conn.feed(
                b"POST / HTTP/1.1\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
            )


class TestLimits(unittest.TestCase):
    def test_oversized_header_block(self):
        conn = HttpConnection(max_header_bytes=64)
        with self.assertRaises(HeaderTooLarge):
            conn.feed(b"GET / HTTP/1.1\r\n" + b"X: " + b"a" * 200 + b"\r\n\r\n")

    def test_oversized_header_block_incremental(self):
        conn = HttpConnection(max_header_bytes=64)
        conn.feed(b"GET / HTTP/1.1\r\n")
        with self.assertRaises(HeaderTooLarge):
            conn.feed(b"X: " + b"a" * 200)  # no terminator yet, still caught

    def test_request_line_too_long(self):
        conn = HttpConnection(max_request_line=32)
        with self.assertRaises(RequestLineTooLong):
            conn.feed(b"GET /" + b"a" * 100 + b" HTTP/1.1\r\n")

    def test_too_many_headers(self):
        conn = HttpConnection(max_headers=4)
        block = b"".join(b"X-%d: 1\r\n" % i for i in range(10))
        with self.assertRaises(TooManyHeaders):
            conn.feed(b"GET / HTTP/1.1\r\n" + block + b"\r\n")

    def test_malicious_content_length_rejected_immediately(self):
        conn = HttpConnection(max_body=1024)
        # Huge declared length must error as soon as headers complete,
        # before any body bytes arrive (no unbounded buffering).
        with self.assertRaises(BodyTooLarge):
            conn.feed(
                b"POST / HTTP/1.1\r\nContent-Length: 999999999999\r\n\r\n"
            )

    def test_body_at_limit_ok(self):
        conn = HttpConnection(max_body=16)
        body = b"x" * 16
        reqs = conn.feed(
            b"POST / HTTP/1.1\r\nContent-Length: 16\r\n\r\n" + body
        )
        self.assertEqual(reqs[0].body, body)


class TestMalformedRequestLine(unittest.TestCase):
    def test_missing_version(self):
        with self.assertRaises(BadRequestLine):
            HttpConnection().feed(b"GET /\r\n\r\n")

    def test_too_many_parts(self):
        with self.assertRaises(BadRequestLine):
            HttpConnection().feed(b"GET / HTTP/1.1 extra\r\n\r\n")

    def test_bad_version_token(self):
        with self.assertRaises(BadRequestLine):
            HttpConnection().feed(b"GET / HTTX/1.1\r\n\r\n")

    def test_unsupported_version(self):
        with self.assertRaises(BadRequestLine):
            HttpConnection().feed(b"GET / HTTP/2.0\r\n\r\n")

    def test_empty_line(self):
        with self.assertRaises(BadRequestLine):
            HttpConnection().feed(b"\r\nGET / HTTP/1.1\r\n\r\n")

    def test_double_space(self):
        with self.assertRaises(BadRequestLine):
            HttpConnection().feed(b"GET  / HTTP/1.1\r\n\r\n")


class TestMalformedHeaders(unittest.TestCase):
    def test_header_without_colon(self):
        with self.assertRaises(BadHeader):
            HttpConnection().feed(b"GET / HTTP/1.1\r\nBroken\r\n\r\n")

    def test_obs_fold_rejected(self):
        with self.assertRaises(BadHeader):
            HttpConnection().feed(
                b"GET / HTTP/1.1\r\nX: 1\r\n folded\r\n\r\n"
            )

    def test_space_in_header_name(self):
        with self.assertRaises(BadHeader):
            HttpConnection().feed(b"GET / HTTP/1.1\r\nBad Name: 1\r\n\r\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
