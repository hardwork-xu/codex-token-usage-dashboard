"""Headless collection checks use generated logs, never an account or real history."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
import usage_log


THREAD = "headless-synthetic"


def event(kind, **payload):
    return {"timestamp": "2026-10-16T00:00:00Z", "type": "event_msg", "payload": {"type": kind, **payload}}


def counters(n):
    return {"total_tokens": n, "input_tokens": n - n // 5,
            "output_tokens": n // 5, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "reasoning_output_tokens": 0}


def usage(n, last=None):
    return event("token_count", info={"total_token_usage": counters(n),
                                     "last_token_usage": counters(n if last is None else last)})


def context(model="gpt-6-astra", tier="default"):
    return {"type": "turn_context", "payload": {"turn_id": "turn", "model": model, "service_tier": tier}}


def records(*, finish=True):
    values = [{"type": "session_meta", "payload": {"id": THREAD, "source": "cli"}},
              event("task_started", turn_id="turn"), context(), usage(100)]
    return values + ([event("task_complete", turn_id="turn")] if finish else [])


class HeadlessCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "synthetic.jsonl"
        self.subject = meter.Meter(self.folder)

    def write(self, values, *, append=False, newline=True):
        with self.path.open("a" if append else "w", encoding="utf-8") as stream:
            stream.write("\n".join(json.dumps(value) for value in values) + ("\n" if newline else ""))

    def register(self):
        meter.write_json(self.folder / "registry.json", {THREAD: {"path": str(self.path), "registeredAt": 1}})

    def saved(self):
        return self.subject.archive.load(THREAD)["result"]

    def drain(self, max_passes=100):
        for _ in range(max_passes):
            state = self.subject.collect_usage()
            if not state["pendingTasks"]:
                return state
        self.fail("synthetic log failed to finish headless backfill")

    def test_no_page_http_pricing_or_account_calls_and_mixed_context_is_saved(self):
        self.write(records(finish=False) + [context("gpt-6.1-sol", "priority"), usage(200, 100),
                                           event("task_complete", turn_id="turn")])
        self.register()
        with contextlib.ExitStack() as stack:
            for name in ("snapshot", "refresh", "refresh_titles", "refresh_descendants"):
                stack.enter_context(mock.patch.object(self.subject, name, side_effect=AssertionError(name)))
            for name in ("estimate_turn", "fetch_quota", "local_get"):
                stack.enter_context(mock.patch.object(meter, name, side_effect=AssertionError(name)))
            state = self.drain()
        self.assertEqual(self.subject.last_client, 0)
        self.assertEqual(state["status"], "ready")
        segments = self.saved()["turns"][0]["usageSegments"]
        self.assertEqual([(row["model"], row["serviceTier"], row["tokens"]["total"]) for row in segments],
                         [("gpt-6-astra", "default", 100), ("gpt-6.1-sol", "priority", 100)])
        self.assertEqual(meter.read_json(self.folder / "collection-status.json", {})["savedTasks"], 1)
        self.assertIn(THREAD, self.subject.descendant_roots)

    def test_hook_prefix_is_completed_without_a_page_and_repeated_counts_are_not_added(self):
        filler = [{"type": "response_item", "payload": {"type": "synthetic", "text": "ignored"}}] * 40
        self.write(records(finish=False) + filler + [usage(200, 100), event("task_complete", turn_id="turn")])
        with mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 5):
            meter.register(self.folder, self.path, THREAD)
            self.assertFalse(self.saved()["reading"]["complete"])
            self.drain()
        self.assertTrue(self.saved()["reading"]["complete"])
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 200)
        original = self.saved()
        with mock.patch.object(self.subject.archive, "save", wraps=self.subject.archive.save) as save:
            self.subject.collect_usage()
            save.assert_not_called()
        self.assertEqual(self.saved(), original)

    def test_late_complete_line_and_post_stop_flush_are_collected(self):
        self.write(records(finish=False))
        self.register()
        self.drain()
        self.write([usage(200, 100)], append=True, newline=False)
        state = self.subject.collect_usage()
        self.assertEqual(state["pendingTasks"], 1)
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 100)
        self.subject.collect_usage()
        with mock.patch.object(self.subject.archive, "save", wraps=self.subject.archive.save) as save:
            self.subject.collect_usage()
            save.assert_not_called()
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        self.write([event("task_complete", turn_id="turn")], append=True)
        self.drain()
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 200)
        self.assertEqual(self.saved()["turns"][0]["status"], "completed")

    def test_restart_and_missing_source_keep_archive(self):
        self.write(records())
        self.register()
        self.drain()
        before = self.saved()
        self.path.unlink()
        self.subject = meter.Meter(self.folder)
        state = self.subject.collect_usage()
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["unreadableTasks"], 1)
        self.assertEqual(self.saved(), before)
        self.assertFalse(self.subject.collection_busy())

    def test_failed_archive_write_is_retried_with_unchanged_source(self):
        self.write(records())
        self.register()
        with mock.patch.object(self.subject.archive, "save", side_effect=OSError("synthetic")):
            state = self.subject.collect_usage()
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["writeErrors"], 1)
        self.assertIsNone(self.subject.archive.load(THREAD))
        state = self.subject.collect_usage()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 100)

    def test_retained_complete_regression_stays_partial_instead_of_ready(self):
        self.write(records())
        self.register()
        self.drain()
        replacement = records()
        replacement[3] = usage(50)
        self.write(replacement)
        state = self.subject.collect_usage()
        self.assertEqual((state["status"], state["errorCode"]), ("partial", "source_changed"))
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 100)
        self.assertEqual(self.subject.collect_usage()["status"], "partial")

    def test_same_size_rewrite_is_revalidated_and_identity_is_revoked(self):
        self.write(records())
        self.register()
        self.drain()
        original = self.path.read_text()
        original_stat = self.path.stat()
        self.path.write_text(original.replace(THREAD, "foreign--synthetic"), encoding="utf-8")
        # POSIX ctime detects an overwrite even after restoring mtime. Windows
        # ctime is creation time, so use its normal mtime change as the signal.
        changed_mtime = original_stat.st_mtime_ns + (1_000_000 if os.name == "nt" else 0)
        os.utime(self.path, ns=(original_stat.st_atime_ns, changed_mtime))
        state = self.subject.collect_usage()
        self.assertEqual(state["unreadableTasks"], 1)
        self.assertIsNone(self.subject.archive.load(THREAD))
        self.subject = meter.Meter(self.folder)
        self.subject.collect_usage()
        self.assertIsNone(self.subject.archive.load(THREAD))

    def test_large_ignored_record_does_not_stall_on_unchanged_cursor(self):
        self.write(records(finish=False) + [
            {"type": "response_item", "payload": {"type": "synthetic", "text": "x" * (5 * 1024 * 1024)}},
            usage(200, 100), event("task_complete", turn_id="turn")])
        self.register()
        with mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 1):
            self.drain(max_passes=200)
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 200)

    def test_changed_speed_windows_rebuild_even_after_snapshot_was_called(self):
        self.write(records())
        self.register()
        self.drain()
        reader = self.subject.cache[THREAD]
        meter.write_json(self.folder / "settings.json", {"pricingMode": "api", "speedOverrides": {"2026-10-16": "fast"},
            "speedOverrideWindows": {"2026-10-16": {"startAt": 1792108800, "endAt": 1792195200, "timeZone": "UTC"}}})
        self.subject.snapshot()
        self.assertEqual(self.subject.collection_sources, {})
        self.drain()
        self.assertIsNot(reader, self.subject.cache[THREAD])
        self.assertEqual(self.saved()["turns"][0]["usageSlices"][0]["speedOverrideDay"], "2026-10-16")

    def test_fair_batches_eventually_reach_every_registered_source(self):
        registry = {}
        for i in range(7):
            identifier = f"synthetic-{i}"
            path = self.folder / f"{identifier}.jsonl"
            values = records()
            values[0]["payload"]["id"] = identifier
            path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")
            registry[identifier] = {"path": str(path)}
        meter.write_json(self.folder / "registry.json", registry)
        with mock.patch.object(meter, "COLLECTION_BATCH", 2), mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 2):
            self.drain()
        self.assertEqual(self.subject.archive.stats()["savedThreads"], 7)

    def test_stale_running_and_bad_sources_do_not_prevent_idle_forever(self):
        self.write(records(finish=False))
        old = time.time() - meter.COLLECTION_ACTIVE_GRACE - 120
        os.utime(self.path, (old, old))
        self.register()
        self.subject.collect_usage()
        self.assertTrue(self.subject.collection_busy())  # actual new backfill
        with mock.patch.object(meter.time, "monotonic", return_value=time.monotonic() + meter.COLLECTION_TAIL_GRACE + 1):
            self.assertFalse(self.subject.collection_busy())
        self.path.unlink()
        self.subject.collect_usage()
        self.subject.collection_busy_until = 0
        self.assertFalse(self.subject.collection_busy())

    def test_live_running_gets_one_hour_lease_and_completion_releases_it(self):
        self.write(records(finish=False))
        self.register()
        self.subject.collect_usage()
        with mock.patch.object(meter.time, "monotonic", return_value=time.monotonic() + 1200):
            self.assertTrue(self.subject.collection_busy())
        self.write([event("task_complete", turn_id="turn")], append=True)
        self.subject.collect_usage()
        with mock.patch.object(meter.time, "monotonic", return_value=time.monotonic() + meter.COLLECTION_TAIL_GRACE + 1):
            self.assertFalse(self.subject.collection_busy())

    def test_idle_server_waits_for_collection_but_stop_remains_available(self):
        server = meter.MeterHTTPServer.__new__(meter.MeterHTTPServer)
        server.activity_lock = threading.Lock()
        server.active_requests = 0
        server.last_request_at = time.monotonic() - 1000
        server.idle_timeout = 1
        server.idle_shutdown_started = False
        server.stop_event = threading.Event()
        server.shutdown = mock.Mock()
        server.meter = self.subject
        self.subject.collection_busy_until = time.monotonic() + 60
        with mock.patch.object(meter.threading, "Thread") as thread:
            server.service_actions()
            thread.assert_not_called()
            self.subject.collection_busy_until = 0
            server.service_actions()
            thread.assert_called_once()

    def test_hook_cli_spawns_wake_without_waiting_for_cold_service(self):
        self.write(records())
        payload = {"hook_event_name": "Stop", "session_id": THREAD, "transcript_path": str(self.path)}
        with mock.patch.object(sys, "argv", ["meter.py", "--data-dir", str(self.folder), "hook"]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(meter.subprocess, "Popen") as spawn, \
                mock.patch.object(meter, "ensure_service", side_effect=AssertionError("must run in child")):
            meter.main()
        self.assertEqual(spawn.call_args.args[0][-1], "wake")
        spawn.return_value.wait.assert_not_called()
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 100)

    def test_wake_failure_is_private_fixed_status_without_exception_content(self):
        with mock.patch.object(meter.subprocess, "Popen", side_effect=OSError("private-canary")):
            meter.wake_service_async(self.folder)
        raw = (self.folder / "collection-wake-status.json").read_text()
        self.assertNotIn("private-canary", raw)
        self.assertEqual(json.loads(raw)["errorCode"], "wake_failed")

    def test_hook_wakes_after_verified_registry_before_potentially_blocking_archive(self):
        self.write(records())
        payload = {"hook_event_name": "Interrupt", "session_id": THREAD, "transcript_path": str(self.path)}
        with mock.patch.object(meter.subprocess, "Popen") as spawn:
            def blocked_save(*args):
                self.assertEqual(meter.read_json(self.folder / "registry.json", {})[THREAD]["path"], str(self.path))
                spawn.assert_called_once()
                raise OSError("synthetic busy database")
            with mock.patch.object(meter.UsageArchive, "save", side_effect=blocked_save):
                self.assertTrue(meter.handle_hook(self.folder, payload, wake_collector=True))
        self.assertEqual(spawn.call_args.args[0][-1], "wake")


if __name__ == "__main__":
    unittest.main()
