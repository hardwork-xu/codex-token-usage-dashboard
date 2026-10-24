"""Normal-source reuse and catch-up tests use only synthetic private fixtures."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
import compressed_sources
import usage_log
from test_headless_collection import THREAD, records, usage, context, event


class PlainSourceReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "synthetic.jsonl"
        self.subject = meter.Meter(self.folder)
        self.write(records())
        meter.write_json(self.folder / "registry.json", {THREAD: {"path": str(self.path), "registeredAt": 1}})

    def write(self, values, append=False):
        with self.path.open("a" if append else "w", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(row) + "\n" for row in values))

    def drain(self):
        for _ in range(100):
            state = self.subject.collect_usage()
            if not state["pendingTasks"]:
                return state
        self.fail("fixture did not finish")

    def saved(self):
        return self.subject.archive.load(THREAD)

    def test_restart_reuses_only_complete_durable_observation_without_reader_or_save(self):
        self.drain()
        saved = self.saved()
        self.subject = meter.Meter(self.folder)
        with mock.patch.object(usage_log.UsageLogReader, "read", side_effect=AssertionError("unchanged source reread")), \
                mock.patch.object(self.subject.archive, "save", side_effect=AssertionError("unchanged archive rewrite")):
            self.assertEqual(self.drain()["pendingTasks"], 0)
        self.assertEqual(self.saved(), saved)
        self.assertEqual(self.subject.last_client, 0)
        evidence = meter.read_json(self.folder / "source-observations.json", {})[THREAD]
        self.assertEqual(evidence["contentHash"], saved["contentHash"])
        self.assertNotIn(str(self.path), json.dumps(evidence))

    def test_restart_append_revalidates_and_preserves_model_speed_and_counters(self):
        self.write(records(finish=False))
        self.drain()
        self.write([context("gpt-6.1-sol", "priority"), usage(200, 100),
                    event("task_complete", turn_id="turn")], append=True)
        self.subject = meter.Meter(self.folder)
        self.drain()
        rows = self.saved()["result"]["turns"][0]["usageSegments"]
        self.assertEqual([(row["model"], row["serviceTier"], row["tokens"]["total"]) for row in rows],
                         [("gpt-6-astra", "default", 100), ("gpt-6.1-sol", "priority", 100)])

    def test_restart_keeps_per_response_compaction_proof_exactly(self):
        from test_response_usage_records import records as receipt_records, THREAD as receipt_thread
        self.write(receipt_records())
        meter.write_json(self.folder / "registry.json", {receipt_thread: {"path": str(self.path)}})
        self.drain()
        saved = self.subject.archive.load(receipt_thread)
        self.subject = meter.Meter(self.folder)
        with mock.patch.object(usage_log.UsageLogReader, "read", side_effect=AssertionError("must reuse")):
            self.drain()
        self.assertEqual(self.subject.archive.load(receipt_thread), saved)
        self.assertEqual([row["responseAccounting"] for row in saved["result"]["turns"][0]["usageSegments"]],
                         ["paired", "compaction", "paired"])

    def test_same_size_rewrite_with_restored_mtime_cannot_reuse_prior_identity(self):
        self.drain()
        saved_stat = self.path.stat()
        original = self.path.read_text()
        self.path.write_text(original.replace(THREAD, "foreign--synthetic"))
        # On Windows, the opened handle supplies metadata-change time while
        # Python's path ctime may remain creation time.
        os.utime(self.path, ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns))
        self.subject = meter.Meter(self.folder)
        self.assertEqual(self.drain()["unreadableTasks"], 1)
        self.assertIsNone(self.saved())

    def test_same_process_rewrite_is_not_skipped_by_collector_source_stamp(self):
        self.drain()
        saved_stat = self.path.stat()
        self.path.write_text(self.path.read_text().replace(THREAD, "foreign--synthetic"))
        os.utime(self.path, ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns))
        self.assertEqual(self.drain()["unreadableTasks"], 1)
        self.assertIsNone(self.saved())

    def test_archive_hash_change_requires_original_reader_validation(self):
        self.drain()
        changed = self.saved()["result"]
        changed["warnings"] = ["synthetic external metadata change"]
        self.subject.archive.save(THREAD, changed)
        self.subject = meter.Meter(self.folder)
        original = usage_log.UsageLogReader.read
        calls = []
        def read(reader, *args, **kwargs):
            calls.append(True)
            return original(reader, *args, **kwargs)
        with mock.patch.object(usage_log.UsageLogReader, "read", read):
            self.drain()
        self.assertTrue(calls)
        self.assertNotIn("synthetic external metadata change", self.saved()["result"]["warnings"])

    def test_invalidated_archive_never_resurrected_by_observation_hash(self):
        self.drain()
        self.subject.archive.invalidate(THREAD)
        self.subject = meter.Meter(self.folder)
        with mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 2):
            self.subject.collect_usage()
            self.assertIsNone(self.saved())
        self.drain()
        self.assertIsNotNone(self.saved())  # only a complete verified new scan

    def test_incomplete_read_and_failed_save_do_not_create_reuse_evidence(self):
        with mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 2):
            self.subject.collect_usage()
        self.assertFalse((self.folder / "source-observations.json").exists())
        self.subject = meter.Meter(self.folder)
        with mock.patch.object(self.subject.archive, "save", side_effect=OSError("synthetic disk failure")):
            self.subject.collect_usage()
        self.assertFalse((self.folder / "source-observations.json").exists())
        self.drain()
        self.assertIn(THREAD, meter.read_json(self.folder / "source-observations.json", {}))

    def test_source_append_during_save_cannot_be_remembered_as_fully_read(self):
        self.write(records(finish=False))
        original = self.subject.archive.save
        def save(*args):
            result = original(*args)
            self.write([usage(200, 100), event("task_complete", turn_id="turn")], append=True)
            return result
        with mock.patch.object(self.subject.archive, "save", side_effect=save):
            self.subject.collect_usage()
        self.assertNotIn(THREAD, self.subject.source_observations)
        self.drain()
        self.assertEqual(self.saved()["result"]["turns"][0]["tokens"]["total"], 200)

    def test_observation_file_write_failure_retries_without_rewriting_usage(self):
        write = meter.write_json
        def fail_evidence(path, value):
            if path.name == "source-observations.json":
                raise OSError("synthetic evidence-write failure")
            return write(path, value)
        with mock.patch.object(meter, "write_json", side_effect=fail_evidence):
            self.drain()
        self.assertTrue(self.subject.source_observations_dirty)
        self.assertIsNotNone(self.saved())
        with mock.patch.object(self.subject.archive, "save", side_effect=AssertionError("usage already durable")):
            self.drain()
        self.assertFalse(self.subject.source_observations_dirty)
        self.assertIn(THREAD, meter.read_json(self.folder / "source-observations.json", {}))

    def test_changed_algorithm_or_speed_windows_force_validation(self):
        self.drain()
        self.subject = meter.Meter(self.folder)
        self.subject.source_algorithm = "different-parser-version"
        original = usage_log.UsageLogReader.read
        budgets = []
        def read(reader, *args, **kwargs):
            budgets.append(kwargs["time_budget"])
            return original(reader, *args, **kwargs)
        with mock.patch.object(usage_log.UsageLogReader, "read", read):
            self.drain()
        self.assertTrue(budgets)

    def test_single_pending_source_gets_full_bounded_share(self):
        original = self.subject._read_registered
        budgets = []
        def read(*args, **kwargs):
            budgets.append(kwargs["time_budget"])
            return original(*args, **kwargs)
        with mock.patch.object(self.subject, "_read_registered", side_effect=read):
            self.drain()
        self.assertEqual(budgets, [meter.COLLECTION_SOURCE_SECONDS])
        self.assertLessEqual(budgets[0], 1)

    def test_snapshot_lock_is_released_between_sources(self):
        second = self.folder / "second.jsonl"
        second.write_text(self.path.read_text().replace(THREAD, "second-synthetic"))
        meter.write_json(self.folder / "registry.json", {
            THREAD: {"path": str(self.path)}, "second-synthetic": {"path": str(second)}})
        class ObservedLock:
            def __init__(self):
                self.depth = 0
                self.releases = 0
            def __enter__(self):
                self.depth += 1
            def __exit__(self, *_):
                self.depth -= 1
                self.releases += 1
        lock = ObservedLock()
        self.subject.snapshot_lock = lock
        original = self.subject._read_registered
        releases = []
        def read(*args, **kwargs):
            releases.append(lock.releases)
            self.assertEqual(lock.depth, 1)
            return original(*args, **kwargs)
        with mock.patch.object(self.subject, "_read_registered", side_effect=read):
            self.drain()
        self.assertGreater(releases[1], releases[0])

    def test_windows_path_creation_time_and_handle_change_time_are_both_bound(self):
        raw = self.path.read_bytes()
        class Stream(io.BytesIO):
            def fileno(self):
                return 42
        common = dict(st_dev=1, st_ino=2, st_size=len(raw), st_mtime_ns=3, st_birthtime_ns=4)
        path_info = SimpleNamespace(**common, st_ctime_ns=4)
        def read_stamp(change):
            # Python 3.10 can report creation time from both stat and fstat.
            handle = SimpleNamespace(**common, st_ctime_ns=4)
            with mock.patch.object(compressed_sources, "sys", SimpleNamespace(platform="win32")), \
                    mock.patch.object(meter, "is_windows", return_value=True), \
                    mock.patch.object(meter, "_file_change_ns", return_value=change), \
                    mock.patch.object(meter, "_open_regular", return_value=Stream(raw)), \
                    mock.patch.object(meter.os, "fstat", return_value=handle), \
                    mock.patch.object(Path, "lstat", return_value=path_info):
                return self.subject._plain_source_stamp(THREAD, self.path)
        first, changed = read_stamp(5), read_stamp(6)
        self.assertIsNotNone(first)
        self.assertEqual(first["ctimeNs"], changed["ctimeNs"])
        self.assertNotEqual(first["handleChangeNs"], changed["handleChangeNs"])

    def test_native_change_time_detects_rewrite_when_all_python_stats_are_unchanged(self):
        raw = self.path.read_bytes()
        altered = raw.replace(THREAD.encode(), b"foreign--synthetic")
        self.assertEqual(len(raw), len(altered))

        class Stream(io.BytesIO):
            def fileno(self):
                return 42

        info = SimpleNamespace(st_dev=1, st_ino=2, st_size=len(raw), st_mtime_ns=3,
                               st_mtime=3e-9, st_birthtime_ns=4, st_ctime_ns=4)
        with mock.patch.object(compressed_sources, "sys", SimpleNamespace(platform="win32")), \
                mock.patch.object(meter, "is_windows", return_value=True), \
                mock.patch.object(meter.os, "fstat", return_value=info), \
                mock.patch.object(Path, "lstat", return_value=info), \
                mock.patch.object(self.subject.compressed_cache, "inspect", return_value=None):
            with mock.patch.object(meter, "_file_change_ns", return_value=5), \
                    mock.patch.object(meter, "_open_regular", side_effect=lambda _: Stream(raw)):
                before = self.subject._source_info(THREAD, self.path)[0]
                self.assertIsNotNone(self.subject._plain_source_stamp(THREAD, self.path))
            with mock.patch.object(meter, "_file_change_ns", return_value=6), \
                    mock.patch.object(meter, "_open_regular", side_effect=lambda _: Stream(altered)):
                changed = self.subject._source_info(THREAD, self.path)[0]
                self.assertEqual(before[:-1], changed[:-1])
                self.assertNotEqual(before[-1], changed[-1])
                with self.assertRaises(compressed_sources.CompressedSourceError) as rejected:
                    self.subject._plain_source_stamp(THREAD, self.path)
                self.assertEqual(rejected.exception.code, "identity_mismatch")

    def test_unavailable_native_change_time_cannot_enable_reuse(self):
        self.subject.cache[THREAD] = object()
        with mock.patch.object(meter, "is_windows", return_value=True), \
                mock.patch.object(meter, "_file_change_ns", side_effect=OSError("synthetic unavailable")):
            with self.assertRaises(OSError):
                self.subject._source_info(THREAD, self.path)
            self.assertIsNone(self.subject._plain_source_stamp(THREAD, self.path))
        self.assertNotIn(THREAD, self.subject.cache)

    def test_windows_native_query_uses_change_time_not_creation_time(self):
        import ctypes

        def fill(handle, kind, buffer, size):
            self.assertEqual((handle, kind, size), (123, 0, 40))
            buffer._obj.CreationTime = 116444736000000001
            buffer._obj.ChangeTime = 116444736012345678
            return 1

        query = mock.Mock(side_effect=fill)
        meter._windows_change_query.cache_clear()
        self.addCleanup(meter._windows_change_query.cache_clear)
        with mock.patch.object(meter, "is_windows", return_value=True), \
                mock.patch.object(ctypes, "WinDLL", create=True,
                                  return_value=SimpleNamespace(GetFileInformationByHandleEx=query)), \
                mock.patch.dict(sys.modules, {"msvcrt": SimpleNamespace(get_osfhandle=lambda descriptor: 123)}):
            self.assertEqual(meter._file_change_ns(42), 1234567800)
            query.side_effect = None
            query.return_value = 0
            with self.assertRaises(OSError):
                meter._file_change_ns(42)


if __name__ == "__main__":
    unittest.main()
