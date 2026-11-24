"""SDK 0.16: snapshoty disku a klony sandboxů (bez sítě – Client.request je nahrazený)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wbxsandbox  # noqa: E402
from wbxsandbox import CloneList, Sandbox, SandboxError, Snapshot  # noqa: E402

SNAP = {"id": "snap_n1_0123456789abcdef", "name": "after-setup", "source_sandbox": "sb_n1_a", "template": "base",
        "vcpu": 2, "mem_mb": 1024, "disk_mb": 4096, "size_bytes": 123456789, "created_at": "2026-11-21T10:00:00Z"}


def client():
    return wbxsandbox.Client("https://x", "k")


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox._from({"id": "sb_n1_a", "template": "base", "state": "running"}, client())

    def test_snapshot_of_sandbox(self):
        with mock.patch.object(wbxsandbox.Client, "request", return_value=SNAP) as r:
            snap = self.sb.snapshot(name="after-setup")
        self.assertEqual(r.call_args.args, ("POST", "/v1/sandboxes/sb_n1_a/snapshots"))
        self.assertEqual(r.call_args.kwargs["body"], {"name": "after-setup"})
        self.assertGreaterEqual(r.call_args.kwargs["timeout"], 300)
        self.assertIsInstance(snap, Snapshot)
        self.assertEqual((snap.id, snap.name, snap.source_sandbox, snap.template, snap.size_bytes, snap.disk_mb),
                         (SNAP["id"], "after-setup", "sb_n1_a", "base", 123456789, 4096))
        self.assertEqual(snap.info["created_at"], "2026-11-21T10:00:00Z")
        with mock.patch.object(wbxsandbox.Client, "request", return_value=SNAP) as r:
            self.sb.snapshot()
        self.assertEqual(r.call_args.kwargs["body"], {})

    def test_list_get_delete(self):
        for answer in ([SNAP], {"snapshots": [SNAP]}):  # holé pole i objekt se seznamem
            with mock.patch.object(wbxsandbox.Client, "request", return_value=answer) as r:
                out = Snapshot.list(url="https://x", api_key="k")
            self.assertEqual(r.call_args.args, ("GET", "/v1/snapshots"))
            self.assertEqual([s.id for s in out], [SNAP["id"]])
        with mock.patch.object(wbxsandbox.Client, "request", return_value=SNAP) as r:
            snap = Snapshot.get(SNAP["id"], url="https://x", api_key="k")
        self.assertEqual(r.call_args.args, ("GET", "/v1/snapshots/" + SNAP["id"]))
        with mock.patch.object(wbxsandbox.Client, "request", return_value=None) as r:
            snap.delete()
        self.assertEqual(r.call_args.args, ("DELETE", "/v1/snapshots/" + SNAP["id"]))
        # už smazaný snapshot: bez chyby; jiná chyba projde
        with mock.patch.object(wbxsandbox.Client, "request", side_effect=SandboxError(404, "not found")):
            snap.delete()
        with mock.patch.object(wbxsandbox.Client, "request", side_effect=SandboxError(403, "off", code="feature_disabled")):
            with self.assertRaises(SandboxError) as e:
                snap.delete()
        self.assertEqual(e.exception.code, "feature_disabled")

    def test_create_from_snapshot(self):
        sb = {"id": "sb_n1_b", "template": "base", "state": "running"}
        with mock.patch.object(wbxsandbox.Client, "request", return_value=sb) as r:
            out = Sandbox.create(from_snapshot=SNAP["id"], timeout=600, env={"A": "1"}, url="https://x", api_key="k")
        body = r.call_args.kwargs["body"]
        self.assertEqual(body["from_snapshot"], SNAP["id"])
        self.assertNotIn("template", body)  # šablona je šablona snapshotu
        self.assertEqual((body["timeout_s"], body["env"]), (600, {"A": "1"}))
        self.assertEqual(out.id, "sb_n1_b")
        # ze snapshotu se kopíruje disk – delší HTTP limit než výchozích 120 s
        self.assertEqual(r.call_args.kwargs["timeout"], 300)
        # Snapshot.create_sandbox použije stejného klienta (adresu a klíč)
        snap = Snapshot._from(SNAP, wbxsandbox.Client("https://y", "k2"))
        with mock.patch.object(wbxsandbox.Client, "request", return_value=sb) as r:
            snap.create_sandbox(timeout=60)
        self.assertEqual(r.call_args.kwargs["body"]["from_snapshot"], SNAP["id"])
        self.assertEqual(r.call_args.args[0], "POST")
        # bez snapshotu zůstává výchozí šablona base
        with mock.patch.object(wbxsandbox.Client, "request", return_value=sb) as r:
            Sandbox.create(url="https://x", api_key="k")
        self.assertEqual(r.call_args.kwargs["body"]["template"], "base")
        self.assertIsNone(r.call_args.kwargs.get("timeout"))  # běžné vytvoření: výchozí limit klienta
        self.assertNotIn("from_snapshot", r.call_args.kwargs["body"])

    def test_clone(self):
        answer = {"snapshot_id": "snap_n1_ffff", "snapshot_kept": True, "sandboxes": [{"id": "sb_n1_c1", "template": "base", "state": "running"},
                                                               {"id": "sb_n1_c2", "template": "base", "state": "running"}],
                  "errors": [{"index": 2, "name": "w-3", "status": 429, "error": "limit reached", "code": "quota"}]}
        with mock.patch.object(wbxsandbox.Client, "request", return_value=answer) as r:
            out = self.sb.clone(count=3, name="w", env={"B": "2"}, timeout=900, keep_snapshot=True, snapshot_name="base-env")
        self.assertEqual(r.call_args.args, ("POST", "/v1/sandboxes/sb_n1_a/clone"))
        body = r.call_args.kwargs["body"]
        self.assertEqual((body["count"], body["keep_snapshot"], body["timeout_s"], body["env"]), (3, True, 900, {"B": "2"}))
        self.assertEqual((body["snapshot_name"], body["name"]), ("base-env", "w"))
        self.assertIsInstance(out, CloneList)
        self.assertEqual([s.id for s in out], ["sb_n1_c1", "sb_n1_c2"])
        self.assertEqual(out.snapshot_id, "snap_n1_ffff")
        self.assertTrue(out.snapshot_kept)
        self.assertEqual(out.errors, [{"index": 2, "name": "w-3", "status": 429, "error": "limit reached", "code": "quota"}])
        self.assertEqual((out.errors[0]["index"], out.errors[0]["name"]), (2, "w-3"))
        # bez timeout se životnost neposílá (kopie ji dědí ze zdroje), stejně chování po ní, třída
        # a síť; bez keep_snapshot se nepošle
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"sandboxes": [answer["sandboxes"][0]]}) as r:
            one = self.sb.clone()
        for k in ("timeout_s", "on_timeout", "idle_timeout_s", "keep_days", "compute_class", "network"):
            self.assertNotIn(k, r.call_args.kwargs["body"])
        self.assertNotIn("keep_snapshot", r.call_args.kwargs["body"])
        self.assertNotIn("snapshot_name", r.call_args.kwargs["body"])
        self.assertEqual((len(one), one.errors, one.snapshot_kept), (1, [], False))
        with mock.patch.object(wbxsandbox.Client, "request", return_value={"snapshot_kept": False, "sandboxes": [answer["sandboxes"][0]]}):
            self.assertFalse(self.sb.clone().snapshot_kept)

    def test_clone_all_failed_and_bad_count(self):
        with mock.patch.object(wbxsandbox.Client, "request",
                               return_value={"sandboxes": [], "errors": [{"error": "snapshot limit", "code": "snapshot_limit", "status": 429}]}):
            with self.assertRaises(SandboxError) as e:
                self.sb.clone(count=2)
        self.assertEqual((e.exception.status, e.exception.code), (429, "snapshot_limit"))
        for bad in (0, 11):
            with self.assertRaises(ValueError):
                self.sb.clone(count=bad)

    def test_exports(self):
        for name in ("Snapshot", "CloneList"):
            self.assertIn(name, wbxsandbox.__all__)


if __name__ == "__main__":
    unittest.main()
