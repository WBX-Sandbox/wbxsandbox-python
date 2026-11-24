"""Bezpečnost a odolnost SDK (audit 24. 11.): přesměrování, https, opakování, ověřené stahování, CLI."""

import hashlib
import http.server
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wbxsandbox  # noqa: E402
from wbxsandbox import SandboxError  # noqa: E402
from wbxsandbox import cli  # noqa: E402


class _Srv:
    """Lokální HTTP server s handlerem fn(handler) – jen 127.0.0.1."""

    def __init__(self, fn):
        self.calls = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.calls.append((self.command, self.path, dict(self.headers), body))
                fn(self)

            do_GET = do_POST = do_PUT = do_DELETE = _any

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _json(h, code, body=b"{}", headers=None):
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class RedirectTest(unittest.TestCase):
    def test_key_not_sent_to_redirect_target(self):
        other = _Srv(lambda h: _json(h, 200, b'{"features": {}}'))
        s = _Srv(lambda h: (h.send_response(307), h.send_header("Location", other.url + "/v1/features"),
                            h.send_header("Content-Length", "0"), h.end_headers()))
        try:
            with self.assertRaises(SandboxError) as cm:
                wbxsandbox.Client(s.url, "wbx_secret").request("GET", "/v1/features")
            self.assertIn("redirect", cm.exception.message)
            self.assertEqual(other.calls, [])
        finally:
            s.close()
            other.close()


class HttpsTest(unittest.TestCase):
    def test_plain_http_only_locally_or_explicitly(self):
        with self.assertRaises(SandboxError):
            wbxsandbox.Client("http://sandbox.wbx.cz", "k")
        self.assertEqual(wbxsandbox.Client("http://localhost:8080", "k").url, "http://localhost:8080")
        self.assertEqual(wbxsandbox.Client("http://sb.internal", "k", allow_insecure=True).url, "http://sb.internal")
        with mock.patch.dict(os.environ, {"WBXSANDBOX_ALLOW_INSECURE": "1"}):
            self.assertEqual(wbxsandbox.Client("http://sb.internal", "k").url, "http://sb.internal")
        self.assertNotIn("k", repr(wbxsandbox.Client("https://x", "k")).replace("key=", ""))

    def test_terminal_refuses_plain_ws(self):
        from wbxsandbox import terminal
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(terminal.connect("sb_x", url="http://sandbox.wbx.cz", api_key="k"), 2)
        self.assertIn("unencrypted", err.getvalue())


class RetryTest(unittest.TestCase):
    def test_get_retried_post_without_key_not(self):
        n = {"c": 0}

        def h(r):
            n["c"] += 1
            _json(r, 503 if n["c"] == 1 else 200, b'{"ok": true}')
        s = _Srv(h)
        try:
            c = wbxsandbox.Client(s.url, "k")
            with mock.patch("time.sleep"):
                self.assertEqual(c.request("GET", "/v1/x"), {"ok": True})
                n["c"] = 0
                with self.assertRaises(SandboxError) as cm:
                    c.request("POST", "/v1/x", body={})
                self.assertEqual(cm.exception.status, 503)
                n["c"] = 0
                self.assertEqual(c.request("POST", "/v1/jobs", body={"idempotency_key": "a"}), {"ok": True})
        finally:
            s.close()

    def test_run_job_always_has_idempotency_key(self):
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"id": "job_1", "status": "queued"}) as r:
            wbxsandbox.run_job("echo", url="https://x", api_key="k")
            wbxsandbox.run_batch([{"command": "a"}], url="https://x", api_key="k")
        self.assertTrue(r.call_args_list[0].kwargs["body"]["idempotency_key"].startswith("py-"))
        self.assertTrue(r.call_args_list[1].kwargs["body"]["idempotency_key"].startswith("py-"))

    def test_run_stream_without_exit_raises(self):
        sb = wbxsandbox.Sandbox._from({"id": "sb_1", "template": "base"}, wbxsandbox.Client("https://x", "k"))
        with mock.patch.object(wbxsandbox.Sandbox, "stream", return_value=iter([{"type": "stdout", "data": "a"}])):
            with self.assertRaises(SandboxError):
                sb.run("x", on_stdout=lambda s: None)


class DownloadTest(unittest.TestCase):
    def _job(self, data, sha):
        def h(r):
            r.send_response(200)
            r.send_header("Content-Type", "application/octet-stream")
            if sha is not None:
                r.send_header("X-WBX-SHA256", sha)
            r.send_header("Content-Length", str(len(data)))
            r.end_headers()
            r.wfile.write(data)
        s = _Srv(h)
        self.addCleanup(s.close)
        return wbxsandbox.Job({"id": "job_1", "status": "completed"}, wbxsandbox.Client(s.url, "k")), s

    def test_streamed_and_verified_with_one_request(self):
        data = os.urandom(3 << 20)
        job, s = self._job(data, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "a.bin")
            self.assertIsNone(job.download("out/a.bin", dest))
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), data)
        self.assertEqual(len(s.calls), 1)  # žádný druhý požadavek na seznam artefaktů
        self.assertEqual(job.download("out/a.bin"), data)

    def test_missing_or_wrong_checksum_is_an_error(self):
        job, _ = self._job(b"abc", None)
        with self.assertRaises(SandboxError):
            job.download("a")
        self.assertEqual(job.download("a", verify=False), b"abc")
        job, _ = self._job(b"abc", "0" * 64)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SandboxError):
                job.download("a", os.path.join(d, "a"))
            self.assertEqual(os.listdir(d), [])


class CliKeyTest(unittest.TestCase):
    def test_key_file_and_stdin(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("wbx_from_file\n")
        self.addCleanup(os.remove, f.name)
        with mock.patch("wbxsandbox.terminal.connect", return_value=0) as c:
            self.assertEqual(cli.main(["connect", "sb_1", "--key-file", f.name]), 0)
            self.assertEqual(c.call_args.kwargs["api_key"], "wbx_from_file")
            with mock.patch.object(sys, "stdin", io.StringIO("wbx_from_stdin\n")):
                cli.main(["connect", "sb_1", "--key-file", "-"])
            self.assertEqual(c.call_args.kwargs["api_key"], "wbx_from_stdin")
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                cli.main(["connect", "sb_1", "--key", "wbx_argv"])
            self.assertIn("Warning", err.getvalue())


if __name__ == "__main__":
    unittest.main()
