"""Testy Python SDK bez sítě: verze, podpis webhooku, rozpočet (měna účtu, starší názvy parametrů).

Spuštění: cd sdk/python && python3 -m unittest discover -s tests
"""
import hashlib
import hmac
import os
import re
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SDK = os.path.dirname(HERE)
sys.path.insert(0, SDK)

import wbxsandbox  # noqa: E402


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


class VersionTest(unittest.TestCase):
    def test_pyproject_matches_package(self):
        m = re.search(r'^version\s*=\s*"([^"]+)"', _read(SDK, "pyproject.toml"), re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), wbxsandbox.__version__)

    def test_same_major_minor_as_platform(self):
        # SDK má vlastní patch verzi, ale řada major.minor odpovídá platformě. Verze platformy:
        # RELEASE ve vydaném balíku (src/sdk/python i sdk/python), v repozitáři Makefile.
        # Bez metadat platformy test selže – nesmí se tiše přeskočit.
        version = platform_version()
        self.assertIsNotNone(version, "platform version not found (RELEASE in the bundle or Makefile in the repository)")
        self.assertEqual(version.split(".")[:2], wbxsandbox.__version__.split(".")[:2])


def platform_version():
    """Verze platformy z RELEASE (balík) nebo z Makefile (repozitář); None = nenalezeno."""
    for rel in (os.path.join(SDK, "..", "..", "RELEASE"), os.path.join(SDK, "..", "..", "..", "RELEASE")):
        if os.path.exists(rel):
            m = re.search(r"^version:\s*(\S+)", _read(rel), re.M)
            if m:
                return m.group(1)
    mk = os.path.join(SDK, "..", "..", "Makefile")
    if os.path.exists(mk):
        m = re.search(r"^VERSION\s*:=\s*(\S+)", _read(mk), re.M)
        if m:
            return m.group(1)
    return None


class PlatformVersionLookupTest(unittest.TestCase):
    def test_reads_release_then_makefile_and_reports_missing(self):
        import tempfile
        global SDK
        orig = SDK
        try:
            with tempfile.TemporaryDirectory() as d:
                SDK = os.path.join(d, "src", "sdk", "python")
                os.makedirs(SDK)
                self.assertIsNone(platform_version())  # nic → None (test výše pak selže)
                with open(os.path.join(d, "RELEASE"), "w") as f:
                    f.write("version: 9.8.7\ncommit: x\n")
                self.assertEqual(platform_version(), "9.8.7")  # src/sdk/python → ../../../RELEASE
            with tempfile.TemporaryDirectory() as d:
                SDK = os.path.join(d, "sdk", "python")
                os.makedirs(SDK)
                with open(os.path.join(d, "Makefile"), "w") as f:
                    f.write("VERSION := 1.2.3\n")
                self.assertEqual(platform_version(), "1.2.3")
                with open(os.path.join(d, "RELEASE"), "w") as f:
                    f.write("version: 1.2.4\n")
                self.assertEqual(platform_version(), "1.2.4")  # RELEASE má přednost
        finally:
            SDK = orig


class WebhookTest(unittest.TestCase):
    def sign(self, secret, t, body):
        return "t=%d,v1=%s" % (t, hmac.new(secret.encode(), b"%d." % t + body, hashlib.sha256).hexdigest())

    def test_valid_tampered_and_old(self):
        body = b'{"event":"job.finished","id":"job_1"}'
        now = int(time.time())
        sig = self.sign("whsec_x", now, body)
        self.assertTrue(wbxsandbox.verify_webhook("whsec_x", body, sig))
        self.assertFalse(wbxsandbox.verify_webhook("whsec_x", body + b" ", sig))
        self.assertFalse(wbxsandbox.verify_webhook("whsec_y", body, sig))
        self.assertFalse(wbxsandbox.verify_webhook("whsec_x", body, self.sign("whsec_x", now - 3600, body)))
        self.assertFalse(wbxsandbox.verify_webhook("whsec_x", body, "garbage"))


class BudgetTest(unittest.TestCase):
    def call(self, **kw):
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"currency": "CZK"}) as req:
            wbxsandbox.set_budget(url="https://x", api_key="k", **kw)
        return req.call_args

    def test_limits_in_billing_currency(self):
        args = self.call(monthly="500", daily=0, alert_pct=90)
        self.assertEqual(args.args[:2], ("PUT", "/v1/budget"))
        self.assertEqual(args.kwargs["body"], {"monthly_limit": "500", "daily_limit": "0", "alert_pct": 90})

    def test_old_parameter_names_still_work(self):
        args = self.call(monthly_eur="12.5", daily_eur="")
        self.assertEqual(args.kwargs["body"], {"monthly_limit": "12.5", "daily_limit": ""})
        # kladně: starší pořadí pozičních argumentů (měsíc, den) zůstává
        with mock.patch.object(wbxsandbox.Client, "request", return_value={}) as req:
            wbxsandbox.set_budget("7", "1", url="https://x", api_key="k")
        self.assertEqual(req.call_args.kwargs["body"], {"monthly_limit": "7", "daily_limit": "1"})


class PublicURLTest(unittest.TestCase):
    def test_get_url(self):
        c = wbxsandbox.Client(url="https://x", api_key="k")
        sb = wbxsandbox.Sandbox(id="sb_n1_0123456789abcdef", template="base", state="running", _c=c)
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"url": "https://u/"}) as req:
            self.assertEqual(sb.get_url("8080")["url"], "https://u/")
        self.assertEqual(req.call_args.args, ("GET", "/v1/sandboxes/sb_n1_0123456789abcdef/url"))
        self.assertEqual(req.call_args.kwargs["query"], {"port": 8080})


if __name__ == "__main__":
    unittest.main()


class RunCodeTest(unittest.TestCase):
    """run_code: každé volání má vlastní soubor (souběžná volání se nepřepíšou) a po sobě ho smaže."""

    def test_unique_file_per_call_and_cleanup(self):
        files = mock.Mock()
        cmds = []
        with mock.patch.object(wbxsandbox.Sandbox, "files", new_callable=mock.PropertyMock, return_value=files):
            sb = wbxsandbox.Sandbox.__new__(wbxsandbox.Sandbox)
            sb.id = "sb_test"
            sb.run = lambda cmd, timeout=None: cmds.append((cmd, timeout)) or wbxsandbox.CommandResult(0, "", "", 0)
            sb.run_code("print(1)", "python", timeout=5)
            sb.run_code("console.log(1)", "node")
            with self.assertRaises(ValueError):
                sb.run_code("x", "ruby")
        p1, p2 = (c.args[0] for c in files.write.call_args_list)
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.startswith("/tmp/_wbx_code_") and p1.endswith(".py"))
        self.assertTrue(p2.endswith(".js"))
        self.assertEqual(cmds[0], (f"python3 {p1}; rc=$?; rm -f {p1}; exit $rc", 5))
        self.assertTrue(cmds[1][0].startswith(f"node {p2};"))
