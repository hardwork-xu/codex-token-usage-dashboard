"""Source-adapter/archive integration with generated numeric-only fixtures."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
import usage_log
from compressed_sources import CompressedSourceError
from test_headless_collection import THREAD, records, context, usage, event
from test_paginated_usage_archive import header, event as page_event, context as page_context, usage as page_usage, THREAD as PAGE_THREAD


class CompressedIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name).resolve()
        self.original = self.folder / "source.jsonl"
        self.expanded = self.folder / "expanded.jsonl"
        Path(str(self.original) + ".zst").write_bytes(b"synthetic adapter")
        self.write(records())
        meter.write_json(self.folder / "registry.json", {THREAD: {"path": str(self.original)}})
        self.subject = meter.Meter(self.folder)
        self.stamp = {"format": "zstd-v1", "size": 1, "mtimeNs": 1}
        self.adapter(self.subject)

    def write(self, rows):
        self.expanded.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def adapter(self, subject):
        subject.compressed_cache = mock.Mock()
        subject.compressed_cache.inspect.side_effect = lambda *_: dict(self.stamp)
        subject.compressed_cache.resolve.side_effect = lambda *_: {
            "path": str(self.expanded), "sourceStamp": dict(self.stamp)}
        return subject.compressed_cache

    def test_headless_recovery_is_durable_reused_after_restart_and_not_double_counted(self):
        state = self.subject.collect_usage()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(self.subject.archive.load(THREAD)["result"]["turns"][0]["tokens"]["total"], 100)
        self.subject.compressed_cache.release.assert_not_called()
        self.subject._cleanup_compressed_sources()
        self.subject.compressed_cache.release.assert_called_once()
        self.expanded.unlink()
        fresh = meter.Meter(self.folder)
        adapter = self.adapter(fresh)
        state = fresh.snapshot()
        self.assertEqual(state["turns"][0]["tokens"]["total"], 100)
        self.assertEqual(state["conversations"][0]["archive"]["source"], "compressed")
        adapter.resolve.assert_not_called()

    def test_changed_source_is_redecoded_and_model_speed_are_preserved(self):
        self.subject.collect_usage()
        self.stamp["mtimeNs"] = 2
        self.write(records(finish=False) + [context("gpt-6.1-sol", "priority"), usage(200, 100),
                                          event("task_complete", turn_id="turn")])
        self.subject.collect_usage()
        state = self.subject.snapshot()
        self.assertEqual(state["turns"][0]["tokens"]["total"], 200)
        saved = self.subject.archive.load(THREAD)["result"]["turns"][0]
        self.assertEqual([(s["model"], s["serviceTier"]) for s in saved["usageSegments"]],
                         [("gpt-6-astra", "default"), ("gpt-6.1-sol", "priority")])
        self.assertEqual(self.subject.compressed_cache.resolve.call_count, 2)

    def test_incomplete_read_or_failed_save_never_releases_source(self):
        with mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 3):
            self.subject.collect_usage()
        self.subject.compressed_cache.release.assert_not_called()
        self.assertFalse((self.folder / "compressed-observations.json").exists())
        with mock.patch.object(self.subject.archive, "save", side_effect=OSError("synthetic")):
            self.subject.collect_usage()
        self.subject.compressed_cache.release.assert_not_called()
        self.assertFalse((self.folder / "compressed-observations.json").exists())

    def test_changed_archive_hash_requires_source_revalidation(self):
        self.subject.collect_usage()
        self.subject.compressed_observations[THREAD]["contentHash"] = "invalid"
        self.subject._prepare_compressed_source()
        with mock.patch.object(self.subject.archive, "save", wraps=self.subject.archive.save) as save:
            self.subject.snapshot()
        save.assert_called_once()
        self.assertEqual(self.subject.compressed_cache.resolve.call_count, 2)

    def test_verified_archive_read_is_checked_once_without_rewrite_or_fresh_save_time(self):
        self.subject.collect_usage()
        before = self.subject.archive.load(THREAD)
        observations = (self.folder / "compressed-observations.json").read_bytes()
        with mock.patch.object(self.subject.archive, "load", wraps=self.subject.archive.load) as load, \
                mock.patch.object(self.subject.archive, "save", side_effect=AssertionError("unchanged archive rewrite")), \
                mock.patch.object(self.subject, "_remember_compressed", side_effect=AssertionError("already durable")):
            snapshot = self.subject.snapshot()
        load.assert_called_once_with(THREAD)
        self.assertEqual(snapshot["turns"][0]["archiveSavedAt"], before["savedAt"])
        self.assertEqual(snapshot["conversations"][0]["archive"]["savedAt"], before["savedAt"])
        self.assertEqual(snapshot["conversations"][0]["archive"]["source"], "compressed")
        self.assertEqual(self.subject.archive.load(THREAD), before)
        self.assertEqual((self.folder / "compressed-observations.json").read_bytes(), observations)

        fresh = meter.Meter(self.folder)
        self.adapter(fresh)
        with mock.patch.object(fresh.archive, "save", side_effect=AssertionError("unchanged collector rewrite")), \
                mock.patch.object(fresh, "_remember_compressed", side_effect=AssertionError("already durable")):
            collection = fresh.collect_usage()
        self.assertEqual(collection["status"], "ready")
        self.assertEqual(collection["lastSavedAt"], before["savedAt"])

    def test_verified_archive_reprojects_current_speed_window_without_changing_durable_evidence(self):
        self.subject.collect_usage()
        before = self.subject.archive.load(THREAD)
        meter.write_json(self.folder / "settings.json", {
            "pricingMode": "api", "speedOverrides": {"2026-10-16": "fast"},
            "speedOverrideWindows": {"2026-10-16": {
                "startAt": 1792108800, "endAt": 1792195200, "timeZone": "UTC"}}})
        with mock.patch.object(self.subject.archive, "save", side_effect=AssertionError("view is not a new observation")):
            state = self.subject.snapshot()
        self.assertEqual(state["turns"][0]["usageSlices"][0]["speedOverrideDay"], "2026-10-16")
        self.assertEqual(state["turns"][0]["tokens"], before["result"]["turns"][0]["tokens"])
        self.assertEqual(self.subject.archive.load(THREAD), before)

    def test_verified_archive_reuse_preserves_partial_and_pagination_gap(self):
        self.subject.collect_usage()
        result = self.subject.archive.load(THREAD)["result"]
        result["turns"][0].update(quality="partial", note="Synthetic historical coverage gap")
        result["warnings"] = ["Synthetic historical coverage gap"]
        result["continuation"] = {
            "status": "continuation_with_gap", "retainedTurns": 0, "sourceTurns": 1,
            "overlapSegments": 0, "legacyAnchor": False,
            "sourceFirstOrdinal": 10, "sourceLastOrdinal": 20}
        self.subject.archive.save(THREAD, result)
        before = self.subject.archive.load(THREAD)
        self.subject.compressed_observations[THREAD]["contentHash"] = before["contentHash"]
        with mock.patch.object(self.subject.archive, "save", side_effect=AssertionError("must preserve old evidence")):
            state = self.subject.snapshot()
        self.assertEqual(state["turns"][0]["quality"], "partial")
        self.assertEqual(state["turns"][0]["note"], "Synthetic historical coverage gap")
        self.assertEqual(state["monitoring"]["archive"]["status"], "partial")
        self.assertEqual(state["conversations"][0]["archive"]["continuation"], result["continuation"])
        self.assertEqual(state["conversations"][0]["archive"]["savedAt"], before["savedAt"])
        self.assertEqual(self.subject.archive.load(THREAD), before)

    def test_ui_never_copies_full_segments_and_keeps_summary_independent(self):
        self.subject.collect_usage()
        saved = self.subject.archive.load(THREAD)
        class SegmentsWithoutCopy(list):
            def __deepcopy__(self, memo):
                raise AssertionError("UI must not duplicate full event history")
        result = saved["result"]
        result["turns"][0]["usageSegments"] = SegmentsWithoutCopy(result["turns"][0]["usageSegments"])
        archived = {"savedAt": saved["savedAt"], "retained": False, "unchanged": True}
        with mock.patch.object(self.subject, "_read_registered", return_value=(result, None, self.stamp, archived)):
            state = self.subject.snapshot()
        self.assertNotIn("usageSegments", state["turns"][0])
        state["turns"][0]["tokens"]["total"] = 999
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)

    def test_decoder_unavailable_retains_old_numbers_and_explains_missing_dependency(self):
        self.subject.collect_usage()
        self.stamp["mtimeNs"] = 2
        self.subject.compressed_cache.resolve.side_effect = CompressedSourceError("decoder_unavailable")
        self.subject._prepare_compressed_source()
        state = self.subject.snapshot()
        self.assertEqual(state["turns"][0]["tokens"]["total"], 100)
        self.assertIn("Python 3.14", state["conversations"][0]["readError"])
        collected = self.subject.collect_usage()
        self.assertEqual(collected["errorCode"], "decoder_unavailable")
        self.assertEqual(collected["decoderUnavailableTasks"], 1)

    def test_foreign_compressed_identity_invalidates_previous_numbers(self):
        self.subject.collect_usage()
        self.stamp["mtimeNs"] = 2
        self.subject.compressed_cache.resolve.side_effect = CompressedSourceError("identity_mismatch")
        self.subject._prepare_compressed_source()
        self.assertEqual(self.subject.snapshot()["turns"], [])
        self.assertIsNone(self.subject.archive.load(THREAD))

    def test_snapshot_never_cold_decodes_even_with_many_registered_sources(self):
        meter.write_json(self.folder / "registry.json", {
            "cold-" + str(i): {"path": str(self.folder / (str(i) + ".jsonl"))} for i in range(1000)})
        state = self.subject.snapshot()
        self.subject.compressed_cache.resolve.assert_not_called()
        self.assertEqual(state["monitoring"]["coverage"]["backfillingTasks"], 1000)
        self.assertEqual(state["monitoring"]["coverage"]["unreadableTasks"], 0)

    def test_slow_decoder_does_not_hold_snapshot_lock(self):
        started, release = threading.Event(), threading.Event()
        def decode(*args):
            started.set()
            if not release.wait(3):
                raise AssertionError("test decoder was never released")
            return {"path": str(self.expanded), "sourceStamp": dict(self.stamp)}
        self.subject.compressed_cache.resolve.side_effect = decode
        worker = threading.Thread(target=self.subject.collect_usage)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertTrue(self.subject.snapshot_lock.acquire(timeout=0.2))
            self.subject.snapshot_lock.release()
            state = self.subject.snapshot()
            self.assertEqual(state["monitoring"]["coverage"]["backfillingTasks"], 1)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.subject.archive.load(THREAD)["result"]["turns"][0]["tokens"]["total"], 100)

    def test_collector_cold_decode_is_bounded_to_one_per_pass(self):
        other = self.folder / "other.jsonl"
        Path(str(other) + ".zst").write_bytes(b"synthetic")
        meter.write_json(self.folder / "registry.json", {
            THREAD: {"path": str(self.original)}, "other": {"path": str(other)}})
        result = self.subject.collect_usage()
        self.subject.compressed_cache.resolve.assert_called_once()
        self.assertEqual(result["pendingTasks"], 1)

    def test_snapshot_never_waits_for_decoder_cache_release(self):
        self.subject._prepare_compressed_source()
        self.subject.compressed_cache.release.side_effect = AssertionError("release can hold a global decoder lock")
        self.assertEqual(self.subject.snapshot()["turns"][0]["tokens"]["total"], 100)
        self.assertIn(THREAD, self.subject.compressed_release_queue)
        self.assertEqual(self.subject.snapshot()["turns"][0]["tokens"]["total"], 100)
        self.subject.compressed_cache.release.assert_not_called()

    def test_ready_byte_budget_defers_another_cold_decode(self):
        self.subject._prepare_compressed_source()
        meter.write_json(self.folder / "registry.json", {
            THREAD: {"path": str(self.original)}, "other": {"path": str(self.folder / "other.jsonl")}})
        with mock.patch.object(meter, "COMPRESSED_READY_BYTES", 1):
            self.subject._prepare_compressed_source()
        self.subject.compressed_cache.resolve.assert_called_once()

    def test_changed_ready_source_cannot_deadlock_expansion_budget(self):
        self.subject._prepare_compressed_source()
        self.stamp["mtimeNs"] = 2
        with mock.patch.object(meter, "COMPRESSED_READY_BYTES", 1):
            self.subject._prepare_compressed_source()
        self.assertEqual(self.subject.compressed_cache.resolve.call_count, 2)
        self.subject.compressed_cache.release.assert_called_once()
        self.assertEqual(self.subject.compressed_ready[THREAD]["sourceStamp"], self.stamp)

    def test_relocated_ready_source_cannot_deadlock_expansion_budget(self):
        self.subject._prepare_compressed_source()
        replacement = self.folder / "replacement.jsonl"
        meter.write_json(self.folder / "registry.json", {THREAD: {"path": str(replacement)}})
        with mock.patch.object(meter, "COMPRESSED_READY_BYTES", 1):
            self.subject._prepare_compressed_source()
        self.assertEqual(self.subject.compressed_cache.resolve.call_count, 2)
        self.subject.compressed_cache.release.assert_called_once_with(THREAD, self.original, self.stamp)
        self.assertEqual(self.subject.compressed_ready[THREAD]["registeredPath"], str(replacement))

    def test_removed_ready_source_is_released_even_with_empty_registry(self):
        self.subject._prepare_compressed_source()
        meter.write_json(self.folder / "registry.json", {})
        with mock.patch.object(meter, "COMPRESSED_READY_BYTES", 1):
            self.subject._prepare_compressed_source()
        self.assertEqual(self.subject.compressed_ready, {})
        self.subject.compressed_cache.release.assert_called_once_with(THREAD, self.original, self.stamp)

    def test_complete_conflict_releases_raw_cache_but_keeps_numeric_warning(self):
        self.subject.collect_usage()
        self.stamp["mtimeNs"] = 2
        self.write(records()[:3])
        self.subject.collect_usage()
        self.assertIn("result", self.subject.compressed_ready[THREAD])
        self.subject._cleanup_compressed_sources()
        self.expanded.unlink()
        state = self.subject.snapshot()
        self.assertEqual(state["turns"][0]["tokens"]["total"], 100)
        self.assertEqual(state["conversations"][0]["archive"]["reason"], "source_changed")


class PaginatedMeterIntegrationTests(unittest.TestCase):
    def test_snapshot_uses_union_and_keeps_gap_visible_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "source.jsonl"
            def write(rows):
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            write([header(), page_event(1, "task_started", turn_id="one"), page_context(2), page_usage(3, 100),
                   page_event(4, "task_complete", turn_id="one")])
            meter.write_json(folder / "registry.json", {PAGE_THREAD: {"path": str(path)}})
            subject = meter.Meter(folder)
            subject.snapshot()
            write([header(10), page_event(11, "task_started", turn_id="two"), page_context(12, "two"),
                   page_usage(13, 5000), page_event(14, "task_complete", turn_id="two")])
            for reader in (subject, meter.Meter(folder)):
                state = reader.snapshot()
                self.assertEqual(sum(t["tokens"]["total"] for t in state["turns"]), 200)
                self.assertEqual(state["conversations"][0]["archive"]["continuation"]["status"], "continuation_with_gap")
                self.assertEqual(state["monitoring"]["archive"]["status"], "partial")
                self.assertTrue(state["conversations"][0]["reading"]["complete"])
                self.assertEqual(reader.collect_usage()["status"], "partial")


if __name__ == "__main__":
    unittest.main()
