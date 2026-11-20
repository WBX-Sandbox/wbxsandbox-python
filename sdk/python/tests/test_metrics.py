"""Testy Python SDK bez sítě: historie a stream metrik sandboxu, features().

Spuštění: cd sdk/python && python3 -m unittest discover -s tests
"""
import io
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wbxsandbox  # noqa: E402

SB = "sb_n1_0123456789abcdef"


def sandbox():
    return wbxsandbox.Sandbox(id=SB, template="base", state="running", _c=wbxsandbox.Client(url="https://x", api_key="k"))


class FakeResp(io.BytesIO):
    """Odpověď streamu: iterace po řádcích (bajty), kontextový manažer."""


class MetricsHistoryTest(unittest.TestCase):
    def test_history_and_since(self):
        out = {"source": "host", "interval_s": 10, "retention_s": 3600, "state": "running", "samples": [{"t": 5, "cpu_pct": 1.5}]}
        with mock.patch.object(wbxsandbox.Client, "request", return_value=out) as req:
            self.assertEqual(sandbox().metrics_history()["samples"][0]["cpu_pct"], 1.5)
        self.assertEqual(req.call_args.args, ("GET", f"/v1/sandboxes/{SB}/metrics/history"))
        self.assertIsNone(req.call_args.kwargs["query"])
        with mock.patch.object(wbxsandbox.Client, "request", return_value=out) as req:
            sandbox().metrics_history(since=1700000000123)
        self.assertEqual(req.call_args.kwargs["query"], {"since": 1700000000123})
        with mock.patch.object(wbxsandbox.Client, "request", return_value=out) as req:
            sandbox().metrics_history(since=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(req.call_args.kwargs["query"], {"since": 1795176000000})
        with mock.patch.object(wbxsandbox.Client, "request", return_value=out) as req:
            sandbox().metrics_history(since=datetime(2026, 11, 20, 12, 0))  # bez zóny = UTC
        self.assertEqual(req.call_args.kwargs["query"], {"since": 1795176000000})


class MetricsStreamTest(unittest.TestCase):
    BODY = (b"event: state\ndata: {\"interval_s\":10,\"state\":\"running\"}\n\n"
            b": ping\n\n"
            b"event: sample\ndata: {\"t\":1,\"cpu_pct\":12.5,\"mem_mb\":300}\n\n"
            b"event: sample\r\ndata: {\"t\":2,\"cpu_pct\":0}\r\n\r\n"
            b"event: end\ndata: {\"reason\":\"timeout\"}\n\n"
            b"event: sample\ndata: {\"t\":3}\n\n")  # po end už nic

    def test_stream_events(self):
        with mock.patch.object(wbxsandbox.Client, "request", return_value=FakeResp(self.BODY)) as req:
            ev = list(sandbox().metrics_stream(since=7))
        self.assertEqual(req.call_args.args, ("GET", f"/v1/sandboxes/{SB}/metrics/stream"))
        self.assertTrue(req.call_args.kwargs["stream"])
        self.assertEqual(req.call_args.kwargs["query"], {"since": 7})
        self.assertEqual([e["event"] for e in ev], ["state", "sample", "sample", "end"])
        self.assertEqual(ev[0]["data"]["state"], "running")
        self.assertEqual(ev[1]["data"], {"t": 1, "cpu_pct": 12.5, "mem_mb": 300})
        self.assertEqual(ev[3]["data"]["reason"], "timeout")

    def test_sleeping_sandbox_ends_right_away(self):
        body = b"event: state\ndata: {\"state\":\"sleeping\"}\n\nevent: end\ndata: {\"reason\":\"not_running\"}\n\n"
        with mock.patch.object(wbxsandbox.Client, "request", return_value=FakeResp(body)):
            ev = list(sandbox().metrics_stream())
        self.assertEqual([(e["event"], e["data"]) for e in ev],
                         [("state", {"state": "sleeping"}), ("end", {"reason": "not_running"})])


class FeaturesTest(unittest.TestCase):
    def test_features(self):
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"features": {"metrics_history": True, "desktop": False}}) as req:
            f = wbxsandbox.features(url="https://x", api_key="k")
        self.assertEqual(req.call_args.args, ("GET", "/v1/features"))
        self.assertEqual(f, {"metrics_history": True, "desktop": False})
        self.assertIn("features", wbxsandbox.__all__)


if __name__ == "__main__":
    unittest.main()
