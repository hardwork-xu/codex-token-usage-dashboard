"""Synthetic pagination, provenance and durable-ledger continuation regressions."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage_archive
import usage_log

THREAD = "synthetic-paginated-thread"


def count(n):
    return {"total_tokens": n, "input_tokens": n * 4 // 5, "output_tokens": n // 5,
            "cached_input_tokens": n // 5, "cache_write_input_tokens": 0,
            "reasoning_output_tokens": n // 10}


def row(ordinal, kind, payload, *, time=None):
    return {"ordinal": ordinal, "timestamp": 1000 + ordinal if time is None else time,
            "type": kind, "payload": payload}


def header(ordinal=0, **payload):
    return row(ordinal, "session_meta", {"id": THREAD, "history_mode": "paginated", "source": "cli", **payload})


def event(ordinal, kind, **payload):
    return row(ordinal, "event_msg", {"type": kind, **payload})


def context(ordinal, turn="one", model="gpt-6-astra", tier="default"):
    return row(ordinal, "turn_context", {"turn_id": turn, "model": model, "service_tier": tier})


def usage(ordinal, total, last=100):
    return event(ordinal, "token_count", info={"total_token_usage": count(total), "last_token_usage": count(last)})


def legacy(result):
    result = deepcopy(result)
    result.pop("sourceHistory", None)
    for turn in result["turns"]:
        turn.pop("startOrdinal", None)
        turn.pop("endOrdinal", None)
        for segment in turn["usageSegments"]:
            segment.pop("eventOrdinal", None)
            segment.pop("eventPart", None)
    return result


class PaginatedUsageArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.path = self.folder / "synthetic.jsonl"
        self.archive = usage_archive.UsageArchive(self.folder)

    def parse(self, rows):
        self.path.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")
        reader = usage_log.UsageLogReader(THREAD)
        while True:
            result = reader.read(self.path)
            if result["reading"]["complete"]:
                return result

    def saved(self):
        return self.archive.load(THREAD)["result"]

    def open_turn(self):
        return self.parse([header(), event(1, "task_started", turn_id="one"), context(2),
                           usage(5, 100), usage(8, 200)])

    def overlapping_page(self):
        return self.parse([header(3), context(4), usage(5, 100), usage(8, 200),
                           context(9, model="gpt-6.1-sol", tier="priority"), usage(10, 300),
                           event(11, "task_complete", turn_id="one")])

    def test_parser_freezes_event_provenance_and_partial_midturn_start(self):
        result = self.overlapping_page()
        turn = result["turns"][0]
        self.assertEqual(result["sourceHistory"], {"mode": "paginated", "firstOrdinal": 3,
                          "lastOrdinal": 11, "eligible": True, "sawRollback": False})
        self.assertIsNone(turn["startedAt"])
        self.assertEqual(turn["quality"], "partial")
        self.assertEqual([(r["eventOrdinal"], r["eventPart"], r["tokens"]["total"]) for r in turn["usageSegments"]],
                         [(5, 1, 100), (8, 1, 100), (10, 1, 100)])
        self.assertEqual(turn["endOrdinal"], 11)

    def test_overlap_union_keeps_models_speed_and_is_idempotent(self):
        old, new = self.open_turn(), self.overlapping_page()
        self.archive.save(THREAD, old)
        outcome = self.archive.save(THREAD, new)
        self.assertTrue(outcome["merged"])
        self.assertEqual(outcome["continuation"]["status"], "overlap_verified")
        turn = self.saved()["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 300)
        self.assertEqual(turn["startedAt"], 1001)
        self.assertEqual([(r["model"], r["serviceTier"]) for r in turn["usageSegments"]],
                         [("gpt-6-astra", "default"), ("gpt-6-astra", "default"), ("gpt-6.1-sol", "priority")])
        again = self.archive.save(THREAD, new)
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["savedAt"], outcome["savedAt"])

    def test_legacy_contiguous_exact_overlap_migrates_provenance(self):
        self.archive.save(THREAD, legacy(self.open_turn()))
        outcome = self.archive.save(THREAD, self.overlapping_page())
        self.assertTrue(outcome["continuation"]["legacyAnchor"])
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 300)
        self.assertTrue(all("eventOrdinal" in row for row in self.saved()["turns"][0]["usageSegments"]))

    def test_disjoint_forward_page_keeps_observed_history_and_explicit_gap(self):
        old = self.parse([header(), event(1, "task_started", turn_id="one"), context(2), usage(3, 100),
                          event(4, "task_complete", turn_id="one")])
        new = self.parse([header(10), event(11, "task_started", turn_id="two"), context(12, "two"),
                          usage(13, 5000), event(14, "task_complete", turn_id="two")])
        self.archive.save(THREAD, legacy(old))
        outcome = self.archive.save(THREAD, new)
        self.assertEqual(outcome["continuation"]["status"], "continuation_with_gap")
        self.assertEqual([turn["tokens"]["total"] for turn in self.saved()["turns"]], [100, 100])
        # The unexplained difference up to cumulative 5000 is never fabricated.
        self.assertEqual(sum(t["tokens"]["total"] for t in self.saved()["turns"]), 200)
        self.assertTrue(self.archive.save(THREAD, new)["unchanged"])

    def test_same_turn_forward_ordinals_without_overlap_preserve_a_gap(self):
        old = self.open_turn()
        new = self.parse([header(20), context(21), usage(22, 10000), event(23, "task_complete", turn_id="one")])
        self.archive.save(THREAD, old)
        outcome = self.archive.save(THREAD, new)
        self.assertEqual(outcome["continuation"]["status"], "continuation_with_gap")
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 300)

    def test_legacy_same_turn_without_numeric_overlap_stays_conflicted(self):
        self.archive.save(THREAD, legacy(self.open_turn()))
        prior = self.saved()
        new = self.parse([header(20), context(21), usage(22, 10000)])
        self.assertTrue(self.archive.save(THREAD, new)["retained"])
        self.assertEqual(self.saved(), prior)

    def test_changed_ordinal_event_amount_is_not_silently_spliced(self):
        self.archive.save(THREAD, self.open_turn())
        prior = self.saved()
        conflict = self.parse([header(3), context(4), usage(5, 110, 110), usage(8, 210), usage(10, 310)])
        self.assertTrue(self.archive.save(THREAD, conflict)["retained"])
        self.assertEqual(self.saved(), prior)

    def test_full_source_cannot_replace_union_with_changed_models_or_rollback(self):
        self.archive.save(THREAD, self.open_turn())
        self.archive.save(THREAD, self.overlapping_page())
        prior = self.saved()
        base = [header(), event(1, "task_started", turn_id="one"), context(2), usage(5, 100), usage(8, 200),
                context(9, model="gpt-6.1-sol", tier="priority"), usage(10, 300),
                event(11, "task_complete", turn_id="one")]
        modified = deepcopy(base)
        modified[2] = context(2, model="gpt-6.1-sol", tier="priority")
        for rows in (modified, base + [event(12, "thread_rolled_back", num_turns=1)]):
            outcome = self.archive.save(THREAD, self.parse(rows))
            self.assertTrue(outcome["retained"])
            self.assertEqual(outcome["reason"], "pagination_conflict")
            self.assertEqual(self.saved(), prior)
        outcome = self.archive.save(THREAD, self.parse(base))
        self.assertFalse(outcome["retained"])
        self.assertNotIn("continuation", self.saved())

    def test_verified_full_source_can_fill_gap_without_discarding_old_observations(self):
        self.archive.save(THREAD, self.open_turn())
        suffix = [header(20), context(21), usage(22, 400), event(23, "task_complete", turn_id="one")]
        self.archive.save(THREAD, self.parse(suffix))
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 300)
        full = [header(), event(1, "task_started", turn_id="one"), context(2), usage(5, 100), usage(8, 200),
                context(21), usage(22, 400), event(23, "task_complete", turn_id="one")]
        outcome = self.archive.save(THREAD, self.parse(full))
        self.assertFalse(outcome["retained"])
        self.assertNotIn("continuation", self.saved())
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 400)

    def test_plain_truncation_and_same_window_shrink_remain_conflicts(self):
        for plain in (False, True):
            with self.subTest(plain=plain):
                old = self.open_turn()
                self.archive.save(THREAD, old)
                new = self.parse([header(), event(1, "task_started", turn_id="one"), context(2), usage(5, 100)])
                if plain:
                    new = legacy(new)
                self.assertTrue(self.archive.save(THREAD, new)["retained"])
                self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 200)

    def test_nonzero_same_window_shrink_is_not_mislabeled_continuation(self):
        old = self.overlapping_page()
        self.archive.save(THREAD, old)
        new = self.parse([header(3), context(4), usage(5, 100), usage(8, 200)])
        outcome = self.archive.save(THREAD, new)
        self.assertTrue(outcome["retained"])
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 300)

    def test_revert_or_nonincreasing_ordinal_disables_join(self):
        old = self.open_turn()
        self.archive.save(THREAD, old)
        for extra in (event(24, "thread_rollback"), event(24, "thread_rolled_back", num_turns=1),
                      row(24, "thread_rolled_back", {"num_turns": 1}), event(19, "unused_event")):
            new = self.parse([header(20), event(21, "task_started", turn_id="two"), context(22, "two"),
                              usage(23, 100), extra])
            self.assertFalse(new["sourceHistory"]["eligible"])
            self.assertTrue(self.archive.save(THREAD, new)["retained"])
        self.assertEqual(len(self.saved()["turns"]), 1)

    def test_foreign_identity_is_rejected_before_any_merge(self):
        self.archive.save(THREAD, self.open_turn())
        with self.assertRaises(usage_log.UsageLogError):
            self.parse([header(20), event(21, "task_started", turn_id="two"), context(22, "two"),
                        usage(23, 100), row(24, "session_meta", {"id": "other-thread"})])
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 200)

    def test_disjoint_earlier_timestamps_do_not_pass_on_new_turn_ids(self):
        self.archive.save(THREAD, self.open_turn())
        rows = [header(20), event(21, "task_started", turn_id="two"), context(22, "two"), usage(23, 100)]
        for value in rows:
            value["timestamp"] = 1000
        self.assertTrue(self.archive.save(THREAD, self.parse(rows))["retained"])

    def test_incomplete_page_can_add_observations_then_finish_without_recount(self):
        old = self.open_turn()
        self.archive.save(THREAD, old)
        new = self.overlapping_page()
        new["reading"]["complete"] = False
        new["reading"]["fileBytes"] += 100
        outcome = self.archive.save(THREAD, new)
        self.assertTrue(outcome["merged"])
        self.assertFalse(self.saved()["reading"]["complete"])
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 300)
        new["reading"]["complete"] = True
        new["reading"]["bytesRead"] = new["reading"]["fileBytes"]
        self.archive.save(THREAD, new)
        self.assertEqual(self.saved()["turns"][0]["tokens"]["total"], 300)
        self.assertTrue(self.saved()["reading"]["complete"])

    def test_six_counters_segments_slices_and_daily_buckets_conserve(self):
        self.archive.save(THREAD, self.open_turn())
        self.archive.save(THREAD, self.overlapping_page())
        turn = self.saved()["turns"][0]
        for field, total in turn["tokens"].items():
            self.assertEqual(sum(row["tokens"][field] for row in turn["usageSegments"]), total)
            self.assertEqual(sum(row["tokens"][field] for row in turn["usageSlices"]), total)
            self.assertEqual(sum(row[field] for row in turn["dailyUsage"].values()) + turn["undatedTokens"][field], total)

    def test_midturn_context_then_actual_start_upgrades_empty_orphan(self):
        result = self.parse([header(10), context(11), event(12, "task_started", turn_id="one"), usage(13, 100)])
        self.assertEqual(result["turns"][0]["startedAt"], 1012)
        self.assertEqual(result["turns"][0]["startOrdinal"], 12)
        self.assertEqual(result["turns"][0]["quality"], "complete")

    def test_unrecognized_malformed_line_invalidates_continuation_proof(self):
        self.path.write_text(json.dumps(header(20)) + "\nnot-json\n" + json.dumps(context(22)) + "\n", encoding="utf-8")
        result = usage_log.read_usage_log(self.path, THREAD)
        self.assertFalse(result["sourceHistory"]["eligible"])


if __name__ == "__main__":
    unittest.main()
