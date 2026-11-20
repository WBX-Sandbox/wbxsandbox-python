"""Testy SandboxError (kód, Retry-After, tělo) bez sítě.

Spuštění: cd sdk/python && python3 -m pytest -q
"""
import email.message
import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wbxsandbox  # noqa: E402
from wbxsandbox import SandboxError  # noqa: E402


def http_error(status, body, headers=None):
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://x/v1/x", status, "err", h, io.BytesIO(body))


class SandboxErrorTest(unittest.TestCase):
    def call(self, err):
        c = wbxsandbox.Client("https://x", "k")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SandboxError) as cm:
                c.request("POST", "/v1/sandboxes", body={})
        return cm.exception

    def test_old_constructor_and_str_unchanged(self):
        e = SandboxError(404, "not found")
        self.assertEqual(str(e), "HTTP 404: not found")
        self.assertEqual((e.status, e.message, e.code, e.retry_after, e.body), (404, "not found", None, None, None))

    def test_json_error_keeps_code_body_and_retry_after(self):
        body = b'{"error": "quota reached", "code": "quota", "limit": 5}'
        e = self.call(http_error(429, body, {"Retry-After": "7"}))
        self.assertEqual(e.status, 429)
        self.assertEqual(e.message, "quota reached")
        self.assertEqual(str(e), "HTTP 429: quota reached")
        self.assertEqual(e.code, "quota")
        self.assertEqual(e.retry_after, 7.0)
        self.assertEqual(e.body, {"error": "quota reached", "code": "quota", "limit": 5})

    def test_retry_after_http_date(self):
        e = self.call(http_error(503, b'{"error": "busy"}', {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        self.assertEqual(e.retry_after, 0.0)      # datum v minulosti → 0
        self.assertIsNone(e.code)
        self.assertEqual(wbxsandbox._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470), 10.0)
        self.assertIsNone(wbxsandbox._parse_retry_after("nesmysl"))
        self.assertIsNone(wbxsandbox._parse_retry_after(""))
        self.assertIsNone(wbxsandbox._parse_retry_after(None))

    def test_non_json_body(self):
        e = self.call(http_error(502, b"<html>bad gateway</html>"))
        self.assertEqual((e.status, e.message, e.code, e.retry_after), (502, "<html>bad gateway</html>", None, None))
        self.assertEqual(e.body, "<html>bad gateway</html>")

    def test_json_without_error_field(self):
        e = self.call(http_error(400, b'{"code": "bad"}'))
        self.assertEqual(e.code, "bad")
        self.assertEqual(e.body, {"code": "bad"})
        self.assertEqual(e.message, '{"code": "bad"}')

    def test_connection_error_is_status_0(self):
        e = self.call(urllib.error.URLError("refused"))
        self.assertEqual(e.status, 0)
        self.assertIsNone(e.code)


if __name__ == "__main__":
    unittest.main()
