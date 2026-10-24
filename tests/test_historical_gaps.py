"""Historical-gap acknowledgements use synthetic registered paths only."""
import errno
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import historical_gaps as gaps


class HistoricalGapsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.subject = gaps.HistoricalGaps(self.folder)
        self.source = self.folder / "session.jsonl"
        self.registry = {"synthetic-thread": {"path": str(self.source)}}
        self.archive = mock.Mock()
        self.archive.load.return_value = None

    def ack(self, identifiers=None, **kwargs):
        return self.subject.acknowledge(self.registry, identifiers or ["synthetic-thread"], self.archive, **kwargs)

    def assert_error(self, code, action):
        with self.assertRaises(gaps.HistoricalGapError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)

    def test_absent_ledger_and_new_missing_sources_are_not_auto_acknowledged(self):
        self.assertEqual(self.subject.load(), {"version": 1, "acknowledged": {}})
        state = self.subject.reconcile(self.registry, self.archive)
        self.assertEqual(state["acknowledgedTasks"], 0)
        self.assertEqual(state["candidateThreadIds"], ["synthetic-thread"])
        self.assertFalse(self.subject.path.exists())
        self.ack(now=100)
        self.registry["new-thread"] = {"path": str(self.folder / "new.jsonl")}
        state = self.subject.reconcile(self.registry, self.archive)
        self.assertEqual(state["acknowledgedTasks"], 1)
        self.assertEqual(state["candidateThreadIds"], ["new-thread"])

    def test_explicit_selection_private_schema_unknown_is_not_zero_and_idempotence(self):
        state = self.ack(now=100)
        self.assertEqual((state["acknowledgedTasks"], state["savedTasks"], state["unknownCostTasks"], state["unreadableTasks"]), (1, 0, 1, 1))
        self.assertEqual(state["tasks"], [{"threadId": "synthetic-thread", "acknowledgedAt": 100,
                                          "hasSavedUsage": False, "savedAt": None}])
        raw = self.subject.path.read_bytes()
        self.assertNotIn(str(self.source).encode(), raw)
        ledger = json.loads(raw)
        self.assertEqual(set(ledger), {"version", "acknowledged"})
        entry = ledger["acknowledged"]["synthetic-thread"]
        self.assertEqual(set(entry), {"pathHash", "acknowledgedAt"})
        self.assertRegex(entry["pathHash"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.ack(now=200)["updatedAt"], 100)
        self.assertEqual(self.subject.path.read_bytes(), raw)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.subject.path.stat().st_mode), 0o600)

    def test_valid_saved_usage_preserves_original_save_timestamp_without_amounts(self):
        self.archive.load.return_value = {"savedAt": 42, "result": {
            "threadId": "synthetic-thread", "identityVerified": True, "turns": [{"tokens": {"total": 0}}]}}
        state = self.ack(now=100)
        self.assertEqual((state["savedTasks"], state["unknownCostTasks"]), (1, 0))
        self.assertEqual(state["tasks"][0]["savedAt"], 42)
        self.assertNotIn("tokens", state["tasks"][0])
        self.assertNotIn("amount", state["tasks"][0])

    def test_empty_or_unavailable_archived_turns_remain_unknown(self):
        for turns in ([], [{"tokens": None}]):
            with self.subTest(turns=turns):
                self.archive.load.return_value = {"savedAt": 42, "result": {
                    "threadId": "synthetic-thread", "identityVerified": True, "turns": turns}}
                state = self.ack(now=100)
                self.assertEqual((state["savedTasks"], state["unknownCostTasks"]), (0, 1))
                self.assertFalse(state["tasks"][0]["hasSavedUsage"])
                self.assertEqual(state["tasks"][0]["savedAt"], 42)

    def test_existing_plain_or_compressed_source_cannot_be_acknowledged(self):
        for source in (self.source, Path(str(self.source) + ".zst")):
            with self.subTest(source=source.name):
                source.write_bytes(b"synthetic; even invalid content is not missing")
                self.assert_error("source_not_missing", self.ack)
                source.unlink()
        self.assertFalse(self.subject.path.exists())

    def test_reappearing_plain_or_exact_compressed_source_persistently_clears_ack(self):
        for source in (self.source, Path(str(self.source) + ".zst")):
            with self.subTest(source=source.name):
                self.ack(now=100)
                source.write_bytes(b"synthetic")
                state = self.subject.reconcile(self.registry, self.archive)
                self.assertEqual(state["acknowledgedTasks"], 0)
                self.assertEqual(gaps.HistoricalGaps(self.folder).load()["acknowledged"], {})
                source.unlink()
                state = self.subject.reconcile(self.registry, self.archive)
                self.assertEqual(state["candidateThreadIds"], ["synthetic-thread"])

    def test_changed_path_and_unregistered_id_persistently_clear_ack(self):
        self.ack(now=100)
        self.registry["synthetic-thread"]["path"] = str(self.folder / "moved.jsonl")
        state = self.subject.reconcile(self.registry, self.archive)
        self.assertEqual(state["candidateThreadIds"], ["synthetic-thread"])
        self.assertEqual(self.subject.load()["acknowledged"], {})
        self.ack(now=200)
        self.subject.reconcile({}, self.archive)
        self.assertEqual(self.subject.load()["acknowledged"], {})

    def test_reappearing_source_revocation_survives_rejected_acknowledgement(self):
        self.ack(now=100)
        self.source.write_bytes(b"synthetic")
        self.assert_error("source_not_missing", self.ack)
        self.assertEqual(self.subject.load()["acknowledged"], {})
        self.source.unlink()
        self.assertEqual(self.subject.reconcile(self.registry, self.archive)["candidateThreadIds"], ["synthetic-thread"])

    def test_permission_error_is_not_missing_and_existing_ack_does_not_hide_it(self):
        self.ack(now=100)
        native_lstat = Path.lstat
        def denied(path):
            if path == self.source:
                raise PermissionError(errno.EACCES, "synthetic")
            return native_lstat(path)
        with mock.patch.object(Path, "lstat", denied):
            self.assert_error("source_unreadable", self.ack)
            state = self.subject.reconcile(self.registry, self.archive)
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["acknowledgedTasks"], 0)
        self.assertEqual(state["candidateThreadIds"], [])
        self.assertEqual(state["validationErrors"], [{"threadId": "synthetic-thread", "code": "source_unreadable"}])

    def test_sibling_permission_error_is_not_missing(self):
        native_lstat = Path.lstat
        def denied(path):
            if path == Path(str(self.source) + ".zst"):
                raise PermissionError(errno.EACCES, "synthetic")
            return native_lstat(path)
        with mock.patch.object(Path, "lstat", denied):
            self.assert_error("source_unreadable", self.ack)
        self.assertFalse(self.subject.path.exists())

    def test_archive_error_is_explicit_and_never_converted_to_unknown_or_zero(self):
        self.archive.load.side_effect = ValueError("synthetic invalid archive")
        self.assert_error("archive_unreadable", self.ack)
        state = self.subject.reconcile(self.registry, self.archive)
        self.assertEqual((state["status"], state["unknownCostTasks"], state["errorCount"]), ("error", 0, 1))
        self.assertEqual(state["candidateThreadIds"], [])
        self.assertFalse(self.subject.path.exists())

    def test_malformed_or_oversized_ledger_never_becomes_an_empty_success(self):
        for raw in (b"not-json", b'{}', b'{"version":1,"version":1,"acknowledged":{}}',
                    b'{"version":1,"acknowledged":{"bad":{"pathHash":"x","acknowledgedAt":NaN}}}'):
            with self.subTest(raw=raw):
                self.subject.path.write_bytes(raw)
                self.assert_error("ledger_invalid", self.subject.load)
                self.assert_error("ledger_invalid", lambda: self.subject.reconcile(self.registry, self.archive))
                self.assertEqual(self.subject.path.read_bytes(), raw)
        self.subject.path.write_bytes(b"x" * 20)
        with mock.patch.object(gaps, "MAX_LEDGER_BYTES", 10):
            self.assert_error("ledger_invalid", self.subject.load)

    def test_selection_validation_is_atomic_and_bounded(self):
        for selected in ([], ["unknown-thread"], ["synthetic-thread", "synthetic-thread"], ["invalid/id"],
                         ["thread-" + str(i) for i in range(101)]):
            with self.subTest(selected=selected):
                code = "registration_invalid" if selected == ["unknown-thread"] else "selection_invalid"
                self.assert_error(code, lambda: self.subject.acknowledge(self.registry, selected, self.archive))
        self.registry["existing"] = {"path": str(self.folder / "existing.jsonl")}
        Path(self.registry["existing"]["path"]).write_bytes(b"synthetic")
        self.assert_error("source_not_missing", lambda: self.ack(["synthetic-thread", "existing"]))
        self.assertFalse(self.subject.path.exists())
        for value in (True, -1, float("inf"), float("nan"), 10**1000):
            self.assert_error("timestamp_invalid", lambda: self.ack(now=value))

    def test_atomic_writer_failure_preserves_prior_ledger_and_cleans_temp(self):
        self.ack(now=100)
        original = self.subject.path.read_bytes()
        self.registry["new-thread"] = {"path": str(self.folder / "new.jsonl")}
        with mock.patch.object(gaps.os, "replace", side_effect=OSError("synthetic replace failure")):
            self.assert_error("ledger_unavailable", lambda: self.ack(["new-thread"], now=200))
        self.assertEqual(self.subject.path.read_bytes(), original)
        self.assertEqual(list(self.folder.glob("historical-gaps.*.tmp")), [])

    def test_entry_limit_does_not_overwrite_existing_acknowledgements(self):
        self.ack(now=100)
        original = self.subject.path.read_bytes()
        self.registry["new-thread"] = {"path": str(self.folder / "new.jsonl")}
        with mock.patch.object(gaps, "MAX_ENTRIES", 1):
            self.assert_error("ledger_limit", lambda: self.ack(["new-thread"], now=200))
        self.assertEqual(self.subject.path.read_bytes(), original)

    def test_concurrent_instances_preserve_both_selected_acknowledgements(self):
        self.registry["new-thread"] = {"path": str(self.folder / "new.jsonl")}
        barrier = threading.Barrier(2)
        failures = []
        def run(thread_id):
            try:
                barrier.wait(timeout=2)
                gaps.HistoricalGaps(self.folder).acknowledge(self.registry, [thread_id], self.archive, now=100)
            except Exception as exc:
                failures.append(type(exc).__name__)
        workers = [threading.Thread(target=run, args=(thread_id,)) for thread_id in self.registry]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(set(self.subject.load()["acknowledged"]), set(self.registry))

    def test_ledger_symlink_is_rejected_without_touching_target(self):
        target = self.folder / "other.json"
        target.write_bytes(b"unchanged")
        try:
            self.subject.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assert_error("ledger_invalid", self.subject.load)
        self.assert_error("ledger_unavailable", self.ack)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_missing_identity_or_invalid_registry_path_does_not_authorize_ack(self):
        for registry in (None, [], {"invalid/id": {"path": str(self.source)}}):
            self.assert_error("registry_invalid", lambda: self.subject.reconcile(registry, self.archive))
        self.registry["synthetic-thread"]["path"] = "relative.jsonl"
        self.assert_error("registration_invalid", self.ack)
        self.assertEqual(self.subject.reconcile(self.registry, self.archive)["status"], "error")


if __name__ == "__main__":
    unittest.main()
