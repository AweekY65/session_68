"""Terminal-runnable tests for the in-memory HTTP/1.1 parser.

Run:  python3 -m unittest -v   (or)   python3 test_http_parser.py
No sockets, no network, no external services.
"""

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
    UnsupportedTransferEncoding,
)


def feed_all(conn, data, chunk_size=None):
    """Feed data (optionally in fixed-size chunks) and collect all requests."""
    out = []
    if chunk_size is None:
        out.extend(conn.feed(data))
    else:
        for i in range(0, len(data), chunk_size):
            out.extend(conn.feed(data[i : i + chunk_size]))
    return out


class TestCompleteRequests(unittest.TestCase):
    def test_simple_get(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET /index.html HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(len(reqs), 1)
        req = reqs[0]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.target, "/index.html")
        self.assertEqual(req.version, "1.1")
        self.assertEqual(req.header("host"), "example.com")
        self.assertEqual(req.body, b"")
        self.assertTrue(req.keep_alive)
        self.assertFalse(conn.closed)

    def test_post_with_body(self):
        conn = HttpConnection()
        reqs = conn.feed(
            b"POST /submit HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Content-Length: 11\r\n"
            b"\r\n"
            b"hello=world"
        )
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].method, "POST")
        self.assertEqual(reqs[0].body, b"hello=world")
        self.assertEqual(conn.buffered_bytes, 0)

    def test_methods_are_distinguished(self):
        conn = HttpConnection()
        data = b"".join(
            m + b" /r HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
            for m in (b"GET", b"POST", b"PUT", b"DELETE", b"HEAD", b"OPTIONS", b"PATCH")
        )
        reqs = conn.feed(data)
        self.assertEqual(
            [r.method for r in reqs],
            ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"],
        )

    def test_header_value_whitespace_and_case(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.1\r\nX-Test:   spaced value \t\r\n\r\n")
        self.assertEqual(reqs[0].header("x-test"), "spaced value")


class TestFragmentation(unittest.TestCase):
    RAW = (
        b"POST /api?x=1 HTTP/1.1\r\n"
        b"Host: local\r\n"
        b"Content-Length: 5\r\n"
        b"X-Long-Header: " + b"v" * 100 + b"\r\n"
        b"\r\n"
        b"12345"
    )

    def test_byte_by_byte(self):
        conn = HttpConnection()
        reqs = feed_all(conn, self.RAW, chunk_size=1)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].body, b"12345")
        self.assertEqual(reqs[0].header("X-Long-Header"), "v" * 100)

    def test_every_two_byte_split(self):
        # Feeding every possible 2-way split must behave identically.
        for cut in range(len(self.RAW)):
            conn = HttpConnection()
            reqs = conn.feed(self.RAW[:cut])
            self.assertEqual(reqs, [], "unexpected early completion at cut=%d" % cut)
            reqs = conn.feed(self.RAW[cut:])
            self.assertEqual(len(reqs), 1, "failed at cut=%d" % cut)
            self.assertEqual(reqs[0].body, b"12345")

    def test_half_header_line(self):
        conn = HttpConnection()
        self.assertEqual(conn.feed(b"GET / HTTP/1.1\r\nHost: exa"), [])
        reqs = conn.feed(b"mple.com\r\n\r\n")
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].header("Host"), "example.com")

    def test_partial_body(self):
        conn = HttpConnection()
        conn.feed(b"POST / HTTP/1.1\r\nContent-Length: 10\r\n\r\nabc")
        self.assertEqual(conn.buffered_bytes, 3)
        reqs = conn.feed(b"defghij")
        self.assertEqual(reqs[0].body, b"abcdefghij")


class TestPipelining(unittest.TestCase):
    PIPE = (
        b"GET /one HTTP/1.1\r\nHost: a\r\n\r\n"
        b"POST /two HTTP/1.1\r\nContent-Length: 4\r\n\r\n"
        b"DATA"
        b"GET /three HTTP/1.1\r\nConnection: close\r\n\r\n"
    )

    def test_pipeline_single_feed(self):
        conn = HttpConnection()
        reqs = conn.feed(self.PIPE)
        self.assertEqual([r.target for r in reqs], ["/one", "/two", "/three"])
        self.assertEqual(reqs[1].body, b"DATA")
        self.assertTrue(conn.closed)  # third request said Connection: close

    def test_pipeline_first_request_does_not_swallow_next(self):
        # The first request has no body; bytes of the second request line
        # must remain untouched in the buffer.
        conn = HttpConnection()
        reqs = conn.feed(b"GET /a HTTP/1.1\r\n\r\nGET /b HTTP/1.1\r\n\r\n")
        self.assertEqual([r.target for r in reqs], ["/a", "/b"])
        self.assertEqual(conn.buffered_bytes, 0)

    def test_pipeline_byte_by_byte(self):
        conn = HttpConnection()
        reqs = feed_all(conn, self.PIPE, chunk_size=1)
        self.assertEqual([r.target for r in reqs], ["/one", "/two", "/three"])

    def test_pipeline_split_mid_body(self):
        conn = HttpConnection()
        reqs = feed_all(conn, self.PIPE, chunk_size=7)
        self.assertEqual([r.target for r in reqs], ["/one", "/two", "/three"])
        self.assertEqual(reqs[1].body, b"DATA")


class TestKeepAliveAndClose(unittest.TestCase):
    def test_http11_defaults_to_keep_alive(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.1\r\n\r\n")
        self.assertTrue(reqs[0].keep_alive)
        self.assertFalse(conn.closed)
        reqs = conn.feed(b"GET /next HTTP/1.1\r\n\r\n")
        self.assertEqual(reqs[0].target, "/next")

    def test_http11_connection_close(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n")
        self.assertFalse(reqs[0].keep_alive)
        self.assertTrue(conn.closed)
        with self.assertRaises(ConnectionClosedError):
            conn.feed(b"GET /more HTTP/1.1\r\n\r\n")

    def test_http10_defaults_to_close(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.0\r\n\r\n")
        self.assertFalse(reqs[0].keep_alive)
        self.assertTrue(conn.closed)

    def test_http10_keep_alive_opt_in(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        self.assertTrue(reqs[0].keep_alive)
        self.assertFalse(conn.closed)

    def test_connection_close_case_insensitive(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.1\r\nConnection: Keep-Alive, CLOSE\r\n\r\n")
        self.assertFalse(reqs[0].keep_alive)
        self.assertTrue(conn.closed)

    def test_trailing_data_after_close_reports_completed(self):
        conn = HttpConnection()
        with self.assertRaises(ConnectionClosedError) as ctx:
            conn.feed(b"GET / HTTP/1.1\r\nConnection: close\r\n\r\nGARBAGE")
        self.assertEqual(len(ctx.exception.completed), 1)
        self.assertEqual(ctx.exception.completed[0].target, "/")


class TestContentLengthValidation(unittest.TestCase):
    def _post(self, headers, body=b""):
        conn = HttpConnection()
        return conn.feed(b"POST / HTTP/1.1\r\n" + headers + b"\r\n" + body)

    def test_non_numeric(self):
        with self.assertRaises(InvalidContentLength):
            self._post(b"Content-Length: abc\r\n")

    def test_negative(self):
        with self.assertRaises(InvalidContentLength):
            self._post(b"Content-Length: -5\r\n")

    def test_float(self):
        with self.assertRaises(InvalidContentLength):
            self._post(b"Content-Length: 1.5\r\n")

    def test_empty(self):
        with self.assertRaises(InvalidContentLength):
            self._post(b"Content-Length: \r\n")

    def test_conflicting_duplicates(self):
        with self.assertRaises(ConflictingContentLength):
            self._post(b"Content-Length: 5\r\nContent-Length: 6\r\n")

    def test_conflicting_comma_list(self):
        with self.assertRaises(ConflictingContentLength):
            self._post(b"Content-Length: 5, 6\r\n")

    def test_identical_duplicates_accepted(self):
        reqs = self._post(
            b"Content-Length: 3\r\nContent-Length: 3\r\n", body=b"abc"
        )
        self.assertEqual(reqs[0].body, b"abc")

    def test_identical_comma_list_accepted(self):
        reqs = self._post(b"Content-Length: 3, 3\r\n", body=b"abc")
        self.assertEqual(reqs[0].body, b"abc")

    def test_leading_zeros_match(self):
        reqs = self._post(b"Content-Length: 003\r\nContent-Length: 3\r\n", body=b"abc")
        self.assertEqual(reqs[0].body, b"abc")

    def test_transfer_encoding_rejected(self):
        with self.assertRaises(UnsupportedTransferEncoding) as ctx:
            self._post(b"Transfer-Encoding: chunked\r\n")
        self.assertEqual(ctx.exception.status, 501)


class TestResourceLimits(unittest.TestCase):
    def test_declared_length_above_limit_rejected_immediately(self):
        conn = HttpConnection()
        # 4 GB claim: must error out without allocating anything near it.
        with self.assertRaises(BodyTooLarge) as ctx:
            conn.feed(b"POST / HTTP/1.1\r\nContent-Length: 4294967296\r\n\r\n")
        self.assertEqual(ctx.exception.status, 413)
        self.assertLessEqual(conn.buffered_bytes, 0)

    def test_custom_body_limit(self):
        conn = HttpConnection(max_body_size=4)
        with self.assertRaises(BodyTooLarge):
            conn.feed(b"POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\n")
        reqs = HttpConnection(max_body_size=4).feed(
            b"POST / HTTP/1.1\r\nContent-Length: 4\r\n\r\nabcd"
        )
        self.assertEqual(reqs[0].body, b"abcd")

    def test_header_line_too_long(self):
        conn = HttpConnection()
        with self.assertRaises(HeaderTooLarge):
            conn.feed(b"GET / HTTP/1.1\r\nX: " + b"v" * 9000 + b"\r\n\r\n")

    def test_header_line_too_long_without_crlf(self):
        # Even without a terminator, an ever-growing line must be rejected.
        conn = HttpConnection()
        with self.assertRaises(HeaderTooLarge):
            conn.feed(b"GET / HTTP/1.1\r\nX: " + b"v" * 9000)

    def test_too_many_headers(self):
        conn = HttpConnection(max_header_count=10)
        block = b"".join(b"H%d: v\r\n" % i for i in range(11))
        with self.assertRaises(HeaderTooLarge):
            conn.feed(b"GET / HTTP/1.1\r\n" + block + b"\r\n")

    def test_headers_total_size_limit(self):
        conn = HttpConnection(max_headers_size=1024)
        block = b"".join(b"H%d: %s\r\n" % (i, b"v" * 90) for i in range(20))
        with self.assertRaises(HeaderTooLarge):
            conn.feed(b"GET / HTTP/1.1\r\n" + block + b"\r\n")

    def test_request_line_too_long(self):
        conn = HttpConnection()
        with self.assertRaises(RequestLineTooLong):
            conn.feed(b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\n\r\n")

    def test_request_line_too_long_without_crlf(self):
        conn = HttpConnection()
        with self.assertRaises(RequestLineTooLong):
            conn.feed(b"GET /" + b"a" * 9000)


class TestMalformedRequestLine(unittest.TestCase):
    def _expect_bad(self, raw):
        with self.assertRaises(BadRequestLine):
            HttpConnection().feed(raw)

    def test_missing_version(self):
        self._expect_bad(b"GET /\r\n\r\n")

    def test_extra_spaces(self):
        self._expect_bad(b"GET  /  HTTP/1.1\r\n\r\n")

    def test_tab_separated(self):
        self._expect_bad(b"GET\t/\tHTTP/1.1\r\n\r\n")

    def test_bad_version(self):
        self._expect_bad(b"GET / HTTP/2.0\r\n\r\n")
        self._expect_bad(b"GET / HTTPS/1.1\r\n\r\n")

    def test_bad_method_characters(self):
        self._expect_bad(b"G ET / HTTP/1.1\r\n\r\n")
        self._expect_bad(b"GE(T) / HTTP/1.1\r\n\r\n")

    def test_empty_request_line(self):
        self._expect_bad(b"\r\nGET / HTTP/1.1\r\n\r\n")


class TestMalformedHeaders(unittest.TestCase):
    def _expect_bad_header(self, raw):
        with self.assertRaises(BadHeader):
            HttpConnection().feed(b"GET / HTTP/1.1\r\n" + raw + b"\r\n")

    def test_no_colon(self):
        self._expect_bad_header(b"JustText\r\n")

    def test_space_before_colon(self):
        self._expect_bad_header(b"Host : x\r\n")

    def test_obs_fold_rejected(self):
        self._expect_bad_header(b"X: a\r\n folded\r\n")

    def test_control_char_in_value(self):
        self._expect_bad_header(b"X: a\x00b\r\n")


class TestGetWithBody(unittest.TestCase):
    def test_get_may_carry_content_length_body(self):
        conn = HttpConnection()
        reqs = conn.feed(b"GET / HTTP/1.1\r\nContent-Length: 3\r\n\r\nxyz")
        self.assertEqual(reqs[0].body, b"xyz")

    def test_zero_content_length(self):
        conn = HttpConnection()
        reqs = conn.feed(b"POST / HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
        self.assertEqual(reqs[0].body, b"")
        self.assertEqual(conn.buffered_bytes, 0)


if __name__ == "__main__":
    unittest.main()
