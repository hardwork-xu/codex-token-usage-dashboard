import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from historical_gap_view import decorate_snapshot


class HistoricalGapViewTests(unittest.TestCase):
    def fixture(self):
        snapshot = {"monitoring": {"status": "partial", "message": "historically incomplete",
                    "coverage": {"unreadableTasks": 2, "backfillingTasks": 0},
                    "archive": {"status": "partial", "retainedTasks": 1, "writeErrors": 0},
                    "collection": {"status": "partial", "unreadableTasks": 2, "pendingTasks": 0,
                                   "writeErrors": 0, "errorCode": "source_unavailable"}},
                    "conversations": [{"id": "saved", "readError": "missing", "readErrorCode": "source_missing", "archive": {
                        "source": "archive", "reason": "source_unavailable", "savedAt": 12}},
                        {"id": "unknown", "readError": "missing", "readErrorCode": "source_missing", "archive": None}],
                    "turns": [{"tokens": {"total": 7}, "pricing": {"amount": "0.01"}, "quality": "partial"}],
                    "periods": {"today": {"status": "partial", "amount": "0.01"}}}
        view = {"status": "ready", "tasks": [
            {"threadId": "saved", "hasSavedUsage": True, "acknowledgedAt": 20},
            {"threadId": "unknown", "hasSavedUsage": False, "acknowledgedAt": 20}]}
        sources = {key: {"unreadable": True, "sourceError": "source_missing"} for key in ("saved", "unknown")}
        return snapshot, view, sources

    def test_history_is_distinct_without_changing_amounts_or_original_quality(self):
        snapshot, view, sources = self.fixture()
        original = copy.deepcopy(snapshot)
        decorate_snapshot(snapshot, view, sources)
        self.assertEqual(snapshot["turns"], original["turns"])
        self.assertEqual(snapshot["periods"], original["periods"])
        self.assertEqual(snapshot["monitoring"]["status"], "partial")
        self.assertEqual(snapshot["monitoring"]["activeStatus"], "active")
        self.assertEqual(snapshot["monitoring"]["collection"]["activeStatus"], "ready")
        self.assertIsNone(snapshot["monitoring"]["collection"]["activeErrorCode"])
        self.assertEqual(snapshot["monitoring"]["archive"]["activeRetainedTasks"], 0)
        self.assertFalse(snapshot["conversations"][1]["archive"]["hasSavedUsage"])
        self.assertEqual(snapshot["conversations"][1]["archive"]["source"], "none")

    def test_new_failure_and_failed_write_still_require_attention(self):
        snapshot, view, sources = self.fixture()
        snapshot["conversations"].append({"id": "new", "readError": "missing"})
        sources["new"] = {"unreadable": True}
        snapshot["monitoring"]["collection"]["unreadableTasks"] = 3
        decorate_snapshot(snapshot, view, sources)
        self.assertEqual(snapshot["monitoring"]["activeStatus"], "partial")
        self.assertEqual(snapshot["monitoring"]["collection"]["activeUnreadableTasks"], 1)
        snapshot["monitoring"]["archive"]["writeErrors"] = 1
        decorate_snapshot(snapshot, view, sources)
        self.assertEqual(snapshot["monitoring"]["activeStatus"], "error")

    def test_backfill_and_continuation_remain_visible(self):
        snapshot, view, sources = self.fixture()
        snapshot["conversations"].append({"id": "new", "reading": {"complete": False},
            "archive": {"source": "archive", "reason": "backfilling"}})
        snapshot["monitoring"]["collection"]["pendingTasks"] = 1
        decorate_snapshot(snapshot, view, sources)
        self.assertEqual(snapshot["monitoring"]["activeStatus"], "collecting")
        self.assertEqual(snapshot["monitoring"]["coverage"]["activeBackfillingTasks"], 1)
        snapshot["conversations"][-1]["archive"]["continuation"] = {"status": "continuation_with_gap"}
        decorate_snapshot(snapshot, view, sources)
        self.assertEqual(snapshot["monitoring"]["activeStatus"], "partial")

    def test_broken_acknowledgement_metadata_does_not_claim_healthy_collection(self):
        snapshot, view, sources = self.fixture()
        view["status"] = "error"
        decorate_snapshot(snapshot, view, sources)
        self.assertEqual(snapshot["monitoring"]["activeStatus"], "error")
        self.assertEqual(snapshot["monitoring"]["collection"]["activeErrorCode"], "historical_gap_metadata_error")

    def test_collector_counts_are_from_its_own_source_states(self):
        snapshot, view, sources = self.fixture()
        sources.pop("unknown")
        snapshot["monitoring"]["collection"]["unreadableTasks"] = 1
        decorate_snapshot(snapshot, view, sources)
        self.assertEqual(snapshot["monitoring"]["collection"]["historicalGapTasks"], 1)
        self.assertEqual(snapshot["monitoring"]["collection"]["activeUnreadableTasks"], 0)


if __name__ == "__main__":
    unittest.main()
