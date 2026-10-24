"""Synthetic per-response receipts: no personal logs or account data."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage_archive
import usage_log

THREAD = "synthetic-receipt-thread"
TURN = "synthetic-turn"


def counters(n):
    return {"total_tokens": n, "input_tokens": n * 8 // 10, "output_tokens": n * 2 // 10,
            "cached_input_tokens": n * 4 // 10, "cache_write_input_tokens": 0,
            "reasoning_output_tokens": n // 10}


def row(ordinal, kind, payload):
    return {"ordinal": ordinal, "timestamp": 1000 + ordinal, "type": kind, "payload": payload}


def event(ordinal, kind, **payload):
    return row(ordinal, "event_msg", {"type": kind, **payload})


def context(ordinal, model="gpt-6-astra", tier="default"):
    payload = {"turn_id": TURN, "model": model}
    if tier is not None:
        payload["service_tier"] = tier
    return row(ordinal, "turn_context", payload)


def receipt(ordinal, name, amount=100, total=100, **fields):
    return row(ordinal, "token_usage_record", {"thread_id": THREAD, "turn_id": TURN,
                "session_id": "synthetic-session", "root_turn_id": TURN, "response_id": name,
                "usage": counters(amount), "turn_token_usage": counters(total),
                "thread_token_usage": counters(total), **fields})


def token(ordinal, total=100, last=100, estimate=False):
    last_value = {**counters(0), "total_tokens": last} if estimate else counters(last)
    return event(ordinal, "token_count", info={"total_token_usage": counters(total), "last_token_usage": last_value})


def records(*, start=0, model="gpt-6-astra", tier="default", finish=True):
    rows = [row(start, "session_meta", {"id": THREAD, "history_mode": "paginated", "source": "cli"}),
            event(start + 1, "task_started", turn_id=TURN), context(start + 2, model, tier),
            receipt(start + 3, "response-sampling"), token(start + 4),
            receipt(start + 5, "response-compaction", total=200), row(start + 6, "compacted", {}),
            context(start + 7, model, tier), token(start + 8, last=60, estimate=True),
            receipt(start + 9, "response-next", total=300), token(start + 10, total=200)]
    if finish:
        rows.append(event(start + 11, "task_complete", turn_id=TURN))
    return rows


class ResponseUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "synthetic.jsonl"
        self.archive = usage_archive.UsageArchive(self.folder)

    def parse(self, rows):
        self.path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
        reader = usage_log.UsageLogReader(THREAD)
        while True:
            result = reader.read(self.path)
            if result["reading"]["complete"]:
                return result

    def test_normal_receipt_keeps_legacy_observation_and_never_double_counts(self):
        rows = records()[:5] + [event(20, "task_complete", turn_id=TURN)]
        result = self.parse(rows)
        old = self.parse([item for item in rows if item["type"] != "token_usage_record"])
        segment = result["turns"][0]["usageSegments"][0]
        self.assertEqual(usage_archive._segment_value(segment),
                         usage_archive._segment_value(old["turns"][0]["usageSegments"][0]))
        self.assertEqual((segment["eventOrdinal"], segment["responseOrdinal"]), (4, 3))
        self.assertEqual(segment["responseAccounting"], "paired")
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)

    def test_compaction_receipt_adds_only_actual_usage_not_context_estimate(self):
        result = self.parse(records())
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 300)
        self.assertEqual([segment["eventOrdinal"] for segment in turn["usageSegments"]], [4, 5, 10])
        self.assertEqual([segment["responseAccounting"] for segment in turn["usageSegments"]],
                         ["paired", "compaction", "paired"])
        usage_archive.sanitize_result(THREAD, result)

    def test_later_cumulative_absorption_never_counts_supplemented_receipt_twice(self):
        rows = records()
        rows[10] = token(10, total=300)
        turn = self.parse(rows)["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 300)
        self.assertEqual([s["tokens"]["total"] for s in turn["usageSegments"]], [100, 100, 100])

    def test_compaction_freezes_model_and_speed_before_new_context(self):
        rows = records()
        rows[7] = context(7, model="gpt-6.1-sol", tier="priority")
        result = self.parse(rows)
        segments = result["turns"][0]["usageSegments"]
        self.assertEqual([(s["model"], s["serviceTier"]) for s in segments],
                         [("gpt-6-astra", "default"), ("gpt-6-astra", "default"), ("gpt-6.1-sol", "priority")])

    def test_model_switch_between_response_and_count_does_not_relabel_response(self):
        rows = records()[:4] + [context(4, model="gpt-6.1-sol", tier="priority"), token(5),
                                 event(6, "task_complete", turn_id=TURN)]
        turn = self.parse(rows)["turns"][0]
        self.assertEqual((turn["model"], turn["serviceTier"]), ("gpt-6-astra", "default"))
        usage_archive.sanitize_result(THREAD, self.parse(rows))

    def test_missing_speed_remains_unknown_for_every_receipt(self):
        result = self.parse(records(tier=None))
        self.assertTrue(all(s["serviceTier"] is None and s["pricingMetadataStatus"] == "unknown"
                            for s in result["turns"][0]["usageSegments"]))

    def test_duplicate_response_id_never_adds_again(self):
        rows = records()
        duplicate = deepcopy(rows[5])
        duplicate["ordinal"] = 5.5  # invalid source history still must not duplicate receipt spending
        result = self.parse(rows[:6] + [duplicate] + rows[6:])
        self.assertEqual(result["turns"][0]["tokens"]["total"], 300)

    def test_conflicting_duplicate_response_id_blocks_supplement(self):
        rows = records()
        duplicate = receipt(5, "response-compaction", amount=200, total=300)
        result = self.parse(rows[:6] + [duplicate] + rows[6:])
        self.assertEqual(result["turns"][0]["tokens"]["total"], 200)
        self.assertTrue(any("冲突" in warning for warning in result["warnings"]))

    def test_duplicate_response_with_increased_cumulative_does_not_count_as_new(self):
        rows = records()[:5] + [receipt(5, "response-sampling", total=200), token(6, total=200),
                                event(7, "task_complete", turn_id=TURN)]
        result = self.parse(rows)
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)
        self.assertTrue(any("冲突" in warning for warning in result["warnings"]))

    def test_duplicate_without_notification_cannot_consume_next_unique_response(self):
        rows = records()[:5] + [receipt(5, "response-sampling"), receipt(6, "response-unique", total=200),
                                token(7, total=200), event(8, "task_complete", turn_id=TURN)]
        self.assertEqual(self.parse(rows)["turns"][0]["tokens"]["total"], 200)

    def test_foreign_thread_or_turn_receipt_never_changes_this_thread_baseline(self):
        for field in ("thread_id", "turn_id"):
            with self.subTest(field=field):
                rows = records()
                rows[5]["payload"][field] = "foreign-owner"
                result = self.parse(rows)
                self.assertEqual(result["turns"][0]["tokens"]["total"], 200)

    def test_incoherent_receipt_is_not_repaired_or_guessed(self):
        rows = records()
        rows[5]["payload"]["usage"]["total_tokens"] = 99
        self.assertEqual(self.parse(rows)["turns"][0]["tokens"]["total"], 200)

    def test_unverified_cumulative_receipt_jump_is_not_supplemented(self):
        rows = records()
        rows[5]["payload"]["thread_token_usage"] = counters(300)
        result = self.parse(rows)
        self.assertEqual(result["turns"][0]["tokens"]["total"], 200)
        self.assertIn("连续性", result["turns"][0]["note"])

    def test_context_estimate_without_compaction_does_not_enable_supplement(self):
        rows = records()
        rows[6]["type"] = "world_state"
        self.assertEqual(self.parse(rows)["turns"][0]["tokens"]["total"], 200)

    def test_zero_last_is_not_misread_as_the_compaction_estimate_signature(self):
        rows = records()
        rows[8] = token(8, last=0)
        self.assertEqual(self.parse(rows)["turns"][0]["tokens"]["total"], 200)

    def test_receipt_before_read_boundary_waits_then_pairs_once(self):
        reader = usage_log._Reader(THREAD)
        rows = records()
        for value in rows[:6]:
            reader.handle(value)
        waiting = reader.result()
        self.assertEqual(waiting["turns"][0]["tokens"]["total"], 100)
        self.assertTrue(any("等待" in warning for warning in waiting["warnings"]))
        for value in rows[6:]:
            reader.handle(value)
        self.assertEqual(reader.result()["turns"][0]["tokens"]["total"], 300)

    def test_unmatched_receipt_at_end_stays_an_explicit_gap(self):
        rows = records()[:6] + [event(20, "task_complete", turn_id=TURN)]
        result = self.parse(rows)
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)
        self.assertTrue(any("统计缺口" in warning for warning in result["warnings"]))

    def test_existing_complete_archive_can_add_proven_receipt_inside_old_ordinals(self):
        rows = records(start=10)
        old = self.parse([item for item in rows if item["type"] != "token_usage_record"])
        self.archive.save(THREAD, old)
        outcome = self.archive.save(THREAD, self.parse(rows))
        self.assertFalse(outcome["retained"])
        saved = self.archive.load(THREAD)["result"]
        self.assertEqual(saved["turns"][0]["tokens"]["total"], 300)
        self.assertEqual([s["eventOrdinal"] for s in saved["turns"][0]["usageSegments"]], [14, 15, 20])
        self.assertEqual([s["responseAccounting"] for s in saved["turns"][0]["usageSegments"]],
                         ["paired", "compaction", "paired"])
        self.assertTrue(self.archive.save(THREAD, self.parse(rows))["unchanged"])

    def test_legacy_archive_upgrades_without_relabeling_existing_observations(self):
        rows = records(start=10)
        old = self.parse([item for item in rows if item["type"] != "token_usage_record"])
        old.pop("sourceHistory")
        for turn in old["turns"]:
            turn.pop("startOrdinal", None)
            turn.pop("endOrdinal", None)
            for segment in turn["usageSegments"]:
                segment.pop("eventOrdinal", None)
                segment.pop("eventPart", None)
        self.archive.save(THREAD, old)
        outcome = self.archive.save(THREAD, self.parse(rows))
        self.assertFalse(outcome["retained"])
        self.assertEqual(self.archive.load(THREAD)["result"]["turns"][0]["tokens"]["total"], 300)
        self.assertTrue(self.archive.save(THREAD, self.parse(rows))["unchanged"])

    def test_later_page_without_receipts_does_not_erase_saved_response_provenance(self):
        for start in (0, 10):
            with self.subTest(start=start):
                self.archive = usage_archive.UsageArchive(self.folder / str(start))
                rows = records(start=start)
                self.archive.save(THREAD, self.parse(rows))
                outcome = self.archive.save(THREAD, self.parse([r for r in rows if r["type"] != "token_usage_record"]))
                stored = self.archive.load(THREAD)["result"]["turns"][0]["usageSegments"]
                self.assertEqual([r["responseAccounting"] for r in stored], ["paired", "compaction", "paired"])

    def test_archived_response_provenance_cannot_be_rewritten(self):
        result = self.parse(records(start=10))
        self.archive.save(THREAD, result)
        for key, value in (("responseId", "other-response"), ("responseOrdinal", 99),
                           ("responseAccounting", "compaction")):
            with self.subTest(key=key):
                rewritten = deepcopy(result)
                rewritten["turns"][0]["usageSegments"][0][key] = value
                self.assertTrue(self.archive.save(THREAD, rewritten)["retained"])
        self.assertEqual(self.archive.load(THREAD)["result"]["turns"][0]["tokens"]["total"], 300)


if __name__ == "__main__":
    unittest.main()
