"""SDK 0.18: zveřejněné aplikace a vlastní domény (bez sítě – Client.request je nahrazený)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wbxsandbox  # noqa: E402
from wbxsandbox import App, Sandbox, SandboxError  # noqa: E402

APP = {"name": "dog-club", "url": "https://dog-club.box.wbxbox.net/", "sandbox_id": "sb_n1_a", "port": 8000,
       "start_command": "python3 app.py", "cwd": "/home/user/dog-club", "domains": [], "created_at": "2026-11-26T15:00:00Z"}
DOM = {"domain": "booking.example.com", "state": "pending", "url": "https://booking.example.com/",
       "dns": {"cname": {"type": "CNAME", "name": "booking.example.com", "value": "dog-club.box.wbxbox.net"}, "root": []}}


def client():
    return wbxsandbox.Client("https://x", "k")


class AppTest(unittest.TestCase):
    def test_publish_from_sandbox(self):
        sb = Sandbox._from({"id": "sb_n1_a", "template": "base", "state": "running"}, client())
        with mock.patch.object(wbxsandbox.Client, "request", return_value=APP) as r:
            app = sb.publish("dog-club", 8000, start_command="python3 app.py", cwd="/home/user/dog-club")
        self.assertEqual(r.call_args.args, ("POST", "/v1/apps"))
        self.assertEqual(r.call_args.kwargs["body"], {"sandbox_id": "sb_n1_a", "port": 8000, "name": "dog-club",
                                                      "start_command": "python3 app.py", "cwd": "/home/user/dog-club"})
        self.assertIsInstance(app, App)
        self.assertEqual((app.name, app.url, app.port, app.start_command), ("dog-club", APP["url"], 8000, "python3 app.py"))
        with mock.patch.object(wbxsandbox.Client, "request", return_value=APP) as r:
            App.publish("sb_n1_a", 8000, "dog-club", url="https://x", api_key="k")
        self.assertEqual(r.call_args.kwargs["body"], {"sandbox_id": "sb_n1_a", "port": 8000, "name": "dog-club"})

    def test_list_get_unpublish(self):
        for answer in ({"apps": [APP]}, [APP]):
            with mock.patch.object(wbxsandbox.Client, "request", return_value=answer):
                apps = App.list(url="https://x", api_key="k")
            self.assertEqual([a.name for a in apps], ["dog-club"])
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"apps": [APP]}):
            self.assertEqual(App.get("dog-club", url="https://x", api_key="k").sandbox_id, "sb_n1_a")
            with self.assertRaises(SandboxError) as e:
                App.get("nope", url="https://x", api_key="k")
            self.assertEqual(e.exception.status, 404)
        app = App._from(APP, client())
        with mock.patch.object(wbxsandbox.Client, "request", return_value=None) as r:
            app.unpublish()
        self.assertEqual(r.call_args.args, ("DELETE", "/v1/apps/dog-club"))
        with mock.patch.object(wbxsandbox.Client, "request", side_effect=SandboxError(404, "app not found")):
            app.unpublish()  # už pryč – bez chyby
        with mock.patch.object(wbxsandbox.Client, "request", side_effect=SandboxError(403, "turned off")):
            with self.assertRaises(SandboxError):
                app.unpublish()

    def test_domains(self):
        app = App._from(APP, client())
        with mock.patch.object(wbxsandbox.Client, "request", return_value=DOM) as r:
            d = app.add_domain("booking.example.com")
        self.assertEqual(r.call_args.args, ("POST", "/v1/apps/dog-club/domains"))
        self.assertEqual(r.call_args.kwargs["body"], {"domain": "booking.example.com"})
        self.assertEqual(d["dns"]["cname"]["value"], "dog-club.box.wbxbox.net")
        self.assertEqual([x["domain"] for x in app.domains], ["booking.example.com"])
        with mock.patch.object(wbxsandbox.Client, "request", return_value=None) as r:
            app.remove_domain("Booking.Example.com")
        self.assertEqual(r.call_args.args, ("DELETE", "/v1/apps/dog-club/domains/booking.example.com"))
        self.assertEqual(app.domains, [])


if __name__ == "__main__":
    unittest.main()
