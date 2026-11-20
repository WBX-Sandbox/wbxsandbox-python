"""Testy plochy sandboxu (sandbox.computer) bez sítě.

Spuštění: cd sdk/python && python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wbxsandbox  # noqa: E402
from wbxsandbox import ComputerActionError, Sandbox, SandboxError  # noqa: E402


class FakeResp:
    def __init__(self, data, headers):
        self._data, self.headers = data, headers

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sandbox():
    return Sandbox(id="sb_n_1", template="desktop", _c=wbxsandbox.Client("https://x", "k"))


class ComputerTest(unittest.TestCase):
    def test_status_and_control(self):
        sb = sandbox()
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"available": True}) as req:
            self.assertTrue(sb.computer.status()["available"])
            req.assert_called_with("GET", "/v1/sandboxes/sb_n_1/computer/status")
            sb.computer.control("agent", ttl_s=60)
            req.assert_called_with("POST", "/v1/sandboxes/sb_n_1/computer/control", body={"mode": "agent", "ttl_s": 60})

    def test_screenshot(self):
        sb = sandbox()
        hdr = {"Content-Type": "image/jpeg", "X-WBX-Width": "640", "X-WBX-Height": "400",
               "X-WBX-Screen-Width": "1280", "X-WBX-Screen-Height": "800"}
        with mock.patch.object(wbxsandbox.Client, "request", return_value=FakeResp(b"\xff\xd8", hdr)) as req:
            shot = sb.computer.screenshot(format="jpeg", quality=70, max_width=640)
        req.assert_called_with("GET", "/v1/sandboxes/sb_n_1/computer/screenshot",
                               query={"format": "jpeg", "quality": 70, "max_width": 640}, stream=True)
        self.assertEqual((shot.format, shot.width, shot.height, shot.screen_width, shot.screen_height),
                         ("jpeg", 640, 400, 1280, 800))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jpg")
            shot.save(p)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"\xff\xd8")
        # cokoli jiného než obrázek je chyba (nikdy se nevrátí jako snímek)
        with mock.patch.object(wbxsandbox.Client, "request", return_value=FakeResp(b"<html>", {"Content-Type": "text/html"})):
            with self.assertRaises(SandboxError):
                sb.computer.screenshot()

    def test_actions_and_shortcuts(self):
        sb = sandbox()
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"ok": True, "results": []}) as req:
            sb.computer.actions([{"type": "wait", "ms": 10}], screenshot=True, screenshot_format="jpeg", screenshot_max_width=800)
            _, kw = req.call_args
            self.assertEqual(kw["body"], {"actions": [{"type": "wait", "ms": 10}], "screenshot": True,
                                          "screenshot_format": "jpeg", "screenshot_max_width": 800})
            sb.computer.click(10, 20, button="right", count=2)
            self.assertEqual(req.call_args[1]["body"]["actions"],
                             [{"type": "click", "x": 10, "y": 20, "button": "right", "count": 2}])
            sb.computer.type("ahoj; rm -rf /")
            self.assertEqual(req.call_args[1]["body"]["actions"], [{"type": "type", "text": "ahoj; rm -rf /"}])
            sb.computer.key("ctrl+c")
            sb.computer.scroll(5, 5, dy=-3)
            self.assertEqual(req.call_args[1]["body"]["actions"], [{"type": "scroll", "x": 5, "y": 5, "dy": -3}])
            sb.computer.drag(1, 2, 3, 4)
            sb.computer.move(1, 1)
            sb.computer.double_click(1, 1)
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"ok": False, "error": "actions[0] (click) failed"}):
            with self.assertRaises(SandboxError):
                sb.computer.click(1, 1)

    def test_failed_action_raises_computer_action_error(self):
        sb = sandbox()
        resp = {"ok": False, "error": "actions[0] (click) failed", "failed_index": 0,
                "results": [{"type": "click", "ok": False, "error": "outside the screen"}]}
        with mock.patch.object(wbxsandbox.Client, "request", return_value=resp):
            with self.assertRaises(ComputerActionError) as cm:
                sb.computer.click(1, 1)
            # actions() samo nevyhazuje – vrací odpověď
            self.assertFalse(sb.computer.actions([{"type": "click", "x": 1, "y": 1}])["ok"])
        e = cm.exception
        self.assertIsInstance(e, SandboxError)          # zpětná kompatibilita
        self.assertEqual(e.failed_index, 0)
        self.assertEqual(e.results, resp["results"])
        self.assertEqual(e.status, 200)                 # ne 0 (= chyba spojení)
        self.assertEqual(e.message, "actions[0] (click) failed")
        self.assertIs(e.response, resp)
        self.assertIn("desktop action 0 failed", str(e))
        # bez failed_index: poslední provedená akce
        with mock.patch.object(wbxsandbox.Client, "request",
                               return_value={"ok": False, "results": [{"ok": True}, {"ok": False}]}):
            with self.assertRaises(ComputerActionError) as cm:
                sb.computer.key("Return")
        self.assertEqual(cm.exception.failed_index, 1)
        self.assertEqual(cm.exception.message, "the action failed")

    def test_actions_http_timeout_is_long(self):
        sb = sandbox()
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"ok": True, "results": []}) as req:
            sb.computer.type("x" * 4096)
        self.assertEqual(req.call_args[1]["timeout"], 300)

    def test_scroll_positional_order_is_dy_then_dx(self):
        sb = sandbox()
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"ok": True, "results": []}) as req:
            sb.computer.scroll(5, 5, 3)            # třetí poziční = dy (zachováno kvůli 0.15.9)
            self.assertEqual(req.call_args[1]["body"]["actions"], [{"type": "scroll", "x": 5, "y": 5, "dy": 3}])
            sb.computer.scroll(5, 5, 1, 2)
            self.assertEqual(req.call_args[1]["body"]["actions"], [{"type": "scroll", "x": 5, "y": 5, "dy": 1, "dx": 2}])
            sb.computer.scroll(5, 5, dx=-2)
            self.assertEqual(req.call_args[1]["body"]["actions"], [{"type": "scroll", "x": 5, "y": 5, "dx": -2}])


if __name__ == "__main__":
    unittest.main()
