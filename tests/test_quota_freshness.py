"""Quota cache outages and recovery, without accounts or Codex processes."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter


def observation(updated=1000):
    return {"updatedAt": updated, "error": None, "buckets": [{
        "id": "codex", "label": "Synthetic quota", "windows": [{
            "remainingPercent": 87.5, "usedPercent": 12.5,
            "windowMinutes": 300, "resetsAt": 2000}]}]}


class FreshnessTests(unittest.TestCase):
    def test_fresh_precision_is_preserved_and_input_is_unchanged(self):
        source = observation()
        before = deepcopy(source)
        result = meter.quota_view(source, now=1001, last_attempt=1000)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["expiresAt"], 1120)
        self.assertEqual(result["buckets"][0]["windows"][0]["remainingPercent"], 87.5)
        result["buckets"].clear()
        self.assertEqual(source, before)

    def test_failure_old_timestamp_clock_skew_or_expired_window_never_looks_fresh(self):
        failed = {**observation(), "error": "Synthetic connection failure"}
        reset = observation()
        reset["buckets"][0]["windows"][0]["resetsAt"] = 1001
        for source, now in [(failed, 1001), (observation(), 1120),
                            (observation(2000), 1001), (reset, 1001)]:
            with self.subTest(source=source, now=now):
                result = meter.quota_view(source, now=now)
                self.assertEqual(result["status"], "stale")
                self.assertEqual(result["updatedAt"], source["updatedAt"])

    def test_missing_or_corrupt_cache_is_unavailable_not_zero(self):
        for source in [None, [], {}, {"buckets": []},
                       {**observation(), "updatedAt": None},
                       {**observation(), "updatedAt": 10 ** 400},
                       {**observation(), "updatedAt": float("nan")}]:
            with self.subTest(source=source):
                self.assertEqual(meter.quota_view(source, now=1001)["status"], "unavailable")

    def test_cache_requires_a_successful_fetch_in_this_worker(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(meter.time, "time", return_value=1001):
            folder = Path(folder)
            meter.write_json(folder / "quota.json", observation())
            subject = meter.Meter(folder)
            self.assertEqual(subject.snapshot()["quota"]["status"], "stale")
            with mock.patch.object(meter, "fetch_quota", return_value=observation(1001)):
                subject.refresh()
            self.assertEqual(subject.snapshot()["quota"]["status"], "fresh")

    def test_malformed_cache_remains_serializable_for_the_browser(self):
        source = observation(float("nan"))
        source["buckets"].extend([None, {"windows": [None, {"remainingPercent": float("inf")}] }])
        source["buckets"][0]["windows"][0].update(resetsAt=float("nan"), windowMinutes=float("inf"))
        result = meter.quota_view(source, now=1001)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["updatedAt"])
        json.dumps(result, allow_nan=False)

    def test_failed_refresh_survives_restart_without_changing_success_timestamp(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(meter.time, "time", return_value=1001):
            folder = Path(folder)
            meter.write_json(folder / "quota.json", observation())
            subject = meter.Meter(folder)
            with mock.patch.object(meter, "fetch_quota", side_effect=RuntimeError("Synthetic failure")):
                subject.refresh()
            result = subject.snapshot()["quota"]
            self.assertEqual((result["status"], result["updatedAt"], result["lastAttemptAt"]), ("stale", 1000, 1001))
            restarted = meter.Meter(folder).snapshot()["quota"]
            self.assertEqual(restarted["status"], "stale")
            self.assertEqual(restarted["error"], "Synthetic failure")
            with mock.patch.object(meter.time, "time", return_value=1032), \
                    mock.patch.object(meter, "fetch_quota", return_value=observation(1032)):
                subject.refresh()
                repaired = subject.snapshot()["quota"]
            self.assertEqual(repaired["status"], "fresh")
            self.assertIsNone(repaired["error"])

    def test_async_refresh_exposes_running_and_honest_cooldown(self):
        entered, finish, completed = threading.Event(), threading.Event(), threading.Event()
        def fetch():
            entered.set()
            if not finish.wait(2):
                raise RuntimeError("Synthetic timeout")
            return observation(1001)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(meter.time, "time", return_value=1001), \
                mock.patch.object(meter, "fetch_quota", side_effect=fetch) as call:
            subject = meter.Meter(Path(folder))
            original = subject._refresh_quota
            def run():
                try:
                    original()
                finally:
                    completed.set()
            subject._refresh_quota = run
            try:
                self.assertEqual(subject.start_refresh()["status"], "started")
                self.assertTrue(entered.wait(1))
                self.assertTrue(subject.snapshot()["quota"]["refreshing"])
                self.assertEqual(subject.start_refresh()["status"], "running")
            finally:
                finish.set()
                self.assertTrue(completed.wait(2))
            self.assertEqual(subject.start_refresh(), {"status": "throttled", "retryAfterSeconds": 30})
            self.assertFalse(subject.snapshot()["quota"]["refreshing"])
            self.assertEqual(call.call_count, 1)

    def test_clock_rollback_does_not_block_quota_recovery(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(meter.time, "time", return_value=100), \
                mock.patch.object(meter, "fetch_quota", return_value=observation(100)) as call:
            subject = meter.Meter(Path(folder))
            subject.last_attempt = 1000
            self.assertEqual(subject.refresh()["status"], "started")
            self.assertEqual(subject.snapshot()["quota"]["status"], "fresh")
            call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
