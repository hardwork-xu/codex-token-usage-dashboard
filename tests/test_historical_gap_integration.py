"""Historical acknowledgement failures must not hide current collection errors."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
from test_headless_collection import THREAD, records


class HistoricalGapIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.source = self.folder / "synthetic.jsonl"
        self.registry = self.folder / "registry.json"
        self.ledger = self.folder / "historical-gaps.json"
        self.registration = {THREAD: {"path": str(self.source)}}
        self.source.write_text("".join(json.dumps(row) + "\n" for row in records()), encoding="utf-8")
        meter.write_json(self.registry, self.registration)
        self.subject = meter.Meter(self.folder)
        self.subject.collect_usage()
        self.saved = self.subject.archive.load(THREAD)
        self.source.unlink()
        self.subject.acknowledge_historical_gaps({"threadIds": [THREAD]})
        self.assertEqual(self.subject.collect_usage()["activeStatus"], "ready")
        self.ledger_bytes = self.ledger.read_bytes()

    def assert_registry_error(self):
        state = self.subject.snapshot()
        self.assertEqual(state["monitoring"]["status"], "partial")
        self.assertEqual(state["monitoring"]["activeStatus"], "error")
        gaps = state["monitoring"]["historicalGaps"]
        self.assertEqual(gaps["status"], "error")
        self.assertEqual(gaps["errorCode"], "registry_invalid")
        self.assertIsNone(state["periods"]["today"]["tokens"])
        self.assertIsNone(state["periods"]["today"]["amount"])
        self.assertEqual(self.ledger.read_bytes(), self.ledger_bytes)
        self.assertEqual(self.subject.archive.load(THREAD), self.saved)
        collected = self.subject.collect_usage()
        self.assertEqual(collected["status"], "error")
        self.assertEqual(collected["activeStatus"], "error")
        self.assertEqual(collected["activeErrorCode"], "registry_invalid")
        self.assertEqual(collected["historicalGapTasks"], 0)
        self.assertEqual(meter.read_json(self.folder / "collection-status.json", {})["activeStatus"], "error")
        self.assertEqual(self.ledger.read_bytes(), self.ledger_bytes)

    def test_malformed_registry_preserves_acks_and_never_claims_healthy(self):
        for content in ("not-json", "null", "[]"):
            with self.subTest(content=content):
                self.registry.write_text(content, encoding="utf-8")
                self.assert_registry_error()
        meter.write_json(self.registry, self.registration)
        self.assertEqual(self.subject.collect_usage()["activeStatus"], "ready")
        state = self.subject.snapshot()
        self.assertEqual(state["monitoring"]["activeStatus"], "active")
        self.assertEqual(state["monitoring"]["historicalGaps"]["acknowledgedTasks"], 1)
        self.assertEqual(self.ledger.read_bytes(), self.ledger_bytes)

    def test_permission_failure_is_not_treated_as_missing_even_when_exists_is_false(self):
        original_read = Path.read_text
        original_exists = Path.exists

        def read(path, *args, **kwargs):
            if path == self.registry:
                raise PermissionError("synthetic denied")
            return original_read(path, *args, **kwargs)

        def exists(path):
            return False if path == self.registry else original_exists(path)

        with mock.patch.object(Path, "read_text", read), mock.patch.object(Path, "exists", exists):
            self.assert_registry_error()

    def test_invalid_encoding_registry_is_an_error_without_erasing_ack(self):
        self.registry.write_bytes(b"\xff")
        self.assert_registry_error()

    def test_new_missing_source_is_not_hidden_by_existing_ack(self):
        meter.write_json(self.registry, {**self.registration, "new-missing": {
            "path": str(self.folder / "new-missing.jsonl")}})
        state = self.subject.snapshot()
        self.assertEqual(state["monitoring"]["activeStatus"], "partial")
        self.assertEqual(state["monitoring"]["coverage"]["unreadableTasks"], 2)
        self.assertEqual(state["monitoring"]["coverage"]["activeUnreadableTasks"], 1)
        self.assertEqual(state["monitoring"]["historicalGaps"]["candidateThreadIds"], ["new-missing"])
        self.assertEqual(self.subject.collect_usage()["activeUnreadableTasks"], 1)
        self.assertEqual(self.ledger.read_bytes(), self.ledger_bytes)

    def test_unreadable_acknowledged_source_stays_active_error(self):
        original = Path.lstat

        def stat(path, *args, **kwargs):
            if path == self.source:
                raise PermissionError("synthetic denied")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", stat):
            state = self.subject.snapshot()
        self.assertEqual(state["monitoring"]["activeStatus"], "error")
        self.assertEqual(state["monitoring"]["coverage"]["activeUnreadableTasks"], 1)
        self.assertEqual(state["monitoring"]["historicalGaps"]["acknowledgedTasks"], 0)
        self.assertEqual(self.ledger.read_bytes(), self.ledger_bytes)

    def test_foreign_source_reappearance_revokes_ack_and_cannot_restore_saved_usage(self):
        self.source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "foreign"}}) + "\n",
                               encoding="utf-8")
        state = self.subject.snapshot()
        self.assertEqual(state["monitoring"]["activeStatus"], "partial")
        self.assertEqual(state["monitoring"]["coverage"]["activeUnreadableTasks"], 1)
        self.assertEqual(state["monitoring"]["historicalGaps"]["acknowledgedTasks"], 0)
        self.assertEqual(self.subject.historical_gaps.load()["acknowledged"], {})
        self.assertEqual(state["turns"], [])
        self.assertIsNone(self.subject.archive.load(THREAD))

    def test_new_install_without_registry_still_waits_normally(self):
        with tempfile.TemporaryDirectory() as folder:
            subject = meter.Meter(Path(folder))
            self.assertEqual(subject.collect_usage()["activeStatus"], "ready")
            self.assertEqual(subject.snapshot()["monitoring"]["activeStatus"], "waiting")

    def test_transient_read_failure_cannot_be_folded_by_later_missing_check(self):
        # Acknowledgement reconciliation still sees ENOENT, but this scan saw
        # a different failure. Only an actual missing-source read may be folded.
        with mock.patch.object(self.subject, "_read_registered", side_effect=PermissionError("synthetic denied")):
            state = self.subject.snapshot()
        self.assertEqual(state["conversations"][0]["readErrorCode"], "source_unreadable")
        self.assertEqual(state["monitoring"]["activeStatus"], "partial")
        self.assertEqual(state["monitoring"]["coverage"]["activeUnreadableTasks"], 1)
        with mock.patch.object(self.subject, "_source_info", side_effect=PermissionError("synthetic denied")):
            collected = self.subject.collect_usage()
        self.assertEqual(collected["activeStatus"], "partial")
        self.assertEqual(collected["historicalGapTasks"], 0)
        self.assertEqual(collected["activeUnreadableTasks"], 1)
        self.assertEqual(self.ledger.read_bytes(), self.ledger_bytes)

    def test_collection_status_write_failure_updates_active_error(self):
        original = meter.write_json

        def write(path, value):
            if path.name == "collection-status.json":
                raise OSError("synthetic write failure")
            return original(path, value)

        with mock.patch.object(meter, "write_json", write):
            state = self.subject.collect_usage()
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["activeStatus"], "error")
        self.assertEqual(state["activeErrorCode"], "status_write_failed")
        self.assertEqual(self.subject.snapshot()["monitoring"]["activeStatus"], "error")


if __name__ == "__main__":
    unittest.main()
