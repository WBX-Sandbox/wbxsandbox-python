"""SDK 0.15.5+: výpis úloh a dávek s filtry, mkdir a rekurzivní mazání (bez sítě)."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wbxsandbox  # noqa: E402


class ListTest(unittest.TestCase):
    def test_list_jobs_filters(self):
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"jobs": [{"id": "job_a"}]}) as r:
            out = wbxsandbox.list_jobs(status="failed", batch_id="bat_x", cursor="job_c", limit=5,
                                       since=datetime(2026, 11, 15, 9, 0, tzinfo=timezone(timedelta(hours=1))),
                                       url="http://x", api_key="k")
        self.assertEqual(out, [{"id": "job_a"}])
        self.assertEqual(r.call_args.kwargs["query"], {"limit": 5, "status": "failed", "batch_id": "bat_x",
                                                       "since": "2026-11-15T08:00:00Z", "cursor": "job_c"})

    def test_naive_datetime_is_utc_and_string_passes(self):
        self.assertEqual(wbxsandbox._since(datetime(2026, 1, 2, 3, 4, 5)), "2026-01-02T03:04:05Z")
        self.assertEqual(wbxsandbox._since("2026-11-01T00:00:00Z"), "2026-11-01T00:00:00Z")
        self.assertIsNone(wbxsandbox._since(None))

    def test_list_batches(self):
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"batches": [], "next_cursor": None}) as r:
            self.assertEqual(wbxsandbox.list_batches(since="2026-11-01T00:00:00Z", cursor="bat_b", url="http://x", api_key="k"), [])
        self.assertEqual(r.call_args.args[:2], ("GET", "/v1/batches"))
        self.assertEqual(r.call_args.kwargs["query"], {"limit": 20, "since": "2026-11-01T00:00:00Z", "cursor": "bat_b"})
        self.assertIn("list_batches", wbxsandbox.__all__)


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.sb = wbxsandbox.Sandbox._from({"id": "sb_1", "template": "base"}, wbxsandbox.Client("http://x", "k"))

    def test_mkdir_and_remove(self):
        with mock.patch.object(wbxsandbox.Client, "request", return_value=None) as r:
            self.sb.files.mkdir("/home/user/out")
            self.sb.files.remove("/home/user/a.txt")
            self.sb.files.remove("/home/user/build", recursive=True)
        calls = [(c.args[0], c.args[1], c.kwargs.get("body"), c.kwargs.get("query")) for c in r.call_args_list]
        self.assertEqual(calls, [
            ("POST", "/v1/sandboxes/sb_1/fs/mkdir", {"path": "/home/user/out", "parents": True}, None),
            ("DELETE", "/v1/sandboxes/sb_1/files", None, {"path": "/home/user/a.txt"}),
            ("POST", "/v1/sandboxes/sb_1/fs/remove", {"path": "/home/user/build", "recursive": True}, None),
        ])


if __name__ == "__main__":
    unittest.main()
