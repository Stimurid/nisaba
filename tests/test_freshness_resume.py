"""No-network regression tests: resumes must refresh changed live conversations."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from nisaba.archiver import ArchiveStore, _as_epoch, download_jsons

CID = "6acbcadc-6be4-83e9-9edd-249d026f4a07"


class FakeClient:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.calls = []

    def fetch_conversation(self, cid):
        self.calls.append(cid)
        if self.error:
            raise self.error
        return dict(self.body)


def body(ts, content):
    return {
        "conversation_id": CID,
        "title": "Synthetic provider fixture",
        "update_time": ts,
        "create_time": 1893456000,
        "gizmo_id": "g-p-synthetic",
        "mapping": {"a": {"id": "a", "message": {"content": {"parts": [content]}}}},
        "current_node": "a",
    }


def fixture(root: Path, remote: str):
    store = ArchiveStore(root)
    cdir = store.conv_dir(CID)
    old = body(1893456000.0, "old source")
    original = json.dumps(old, ensure_ascii=False, indent=2).encode("utf-8")
    (cdir / "conversation.json").write_bytes(original)
    (cdir / "conversation.md").write_text("old derived markdown", encoding="utf-8")
    (cdir / "metadata.json").write_text(
        json.dumps({"conversation_id": CID, "update_time": "2030-01-01T00:00:00Z", "json_sha256": hashlib.sha256(original).hexdigest()}),
        encoding="utf-8",
    )
    store.conversations_jsonl.write_text(
        json.dumps({"conversation_id": CID, "update_time": remote}) + "\n", encoding="utf-8",
    )
    progress = {
        "discovered_conversations": [CID],
        "completed_conversation_ids": [CID],
        "completed_markdown_ids": [CID],
        "conversation_status": {CID: "COMPLETE"},
        "retry_counts": {},
    }
    return store, cdir, progress, original


class ResumeFreshnessTests(unittest.TestCase):
    def test_resume_skips_equal_remote_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, cdir, progress, old = fixture(Path(tmp), "2030-01-01T00:00:00Z")
            client = FakeClient(body(1893542400.0, "new source"))
            download_jsons(client, store, progress)
            self.assertEqual(client.calls, [])
            self.assertEqual((cdir / "conversation.json").read_bytes(), old)
            self.assertEqual(progress["conversation_status"][CID], "COMPLETE")

    def test_remote_newer_refreshes_json_versions_and_invalidates_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, cdir, progress, old = fixture(Path(tmp), "2030-01-02T00:00:00Z")
            client = FakeClient(body(1893542400.0, "new source"))
            download_jsons(client, store, progress)
            self.assertEqual(client.calls, [CID])
            self.assertIn("new source", (cdir / "conversation.json").read_text(encoding="utf-8"))
            backup = cdir / ".versions" / ("conversation." + hashlib.sha256(old).hexdigest() + ".json")
            self.assertEqual(backup.read_bytes(), old)
            self.assertNotIn(CID, progress["completed_markdown_ids"])
            self.assertEqual(progress["conversation_refresh_count"], 1)
            meta = json.loads((cdir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["archive_status"], "RAW_REFRESHED_MARKDOWN_PENDING")
            self.assertEqual(meta["previous_json_sha256"], hashlib.sha256(old).hexdigest())

    def test_failed_refresh_preserves_original_source_metadata_and_marks_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, cdir, progress, old = fixture(Path(tmp), "2030-01-02T00:00:00Z")
            before_meta = (cdir / "metadata.json").read_bytes()
            client = FakeClient(error=ValueError("synthetic transport failure"))
            download_jsons(client, store, progress)
            self.assertEqual(client.calls, [CID])
            self.assertEqual((cdir / "conversation.json").read_bytes(), old)
            self.assertEqual((cdir / "metadata.json").read_bytes(), before_meta)
            self.assertEqual(progress["conversation_status"][CID], "REFRESH_PENDING")
            self.assertIn(CID, progress["completed_markdown_ids"])

    def test_missing_metadata_uses_raw_source_update_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, cdir, progress, old = fixture(Path(tmp), "2030-01-02T00:00:00Z")
            (cdir / "metadata.json").unlink()
            client = FakeClient(body(1893542400.0, "new source"))
            download_jsons(client, store, progress)
            self.assertEqual(client.calls, [CID])

    def test_timestamp_timezone_and_fractional_precision(self):
        self.assertEqual(_as_epoch("2030-01-02T00:00:00Z"), 1893542400.0)
        self.assertEqual(_as_epoch("2030-01-02T00:00:00.500Z"), 1893542400.5)
        self.assertEqual(_as_epoch(1893542400.5), 1893542400.5)


if __name__ == "__main__":
    unittest.main()
