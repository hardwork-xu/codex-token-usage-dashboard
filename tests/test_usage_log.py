"""Synthetic tests: no real transcripts, credentials, or configuration are read."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
SPEC = importlib.util.spec_from_file_location("usage_log", Path(__file__).resolve().parents[1] / "scripts" / "usage_log.py")
usage_log = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage_log)

THREAD = "thread-fixture"
WHEN = "2026-09-14T00:00:00Z"
META = {"type": "session_meta", "payload": {"id": THREAD, "cli_version": "synthetic", "source": "test"}}


def counters(input_=80, output=20, cached=10, reasoning=5, cache_write=0):
    return {
        "total_tokens": input_ + output,
        "input_tokens": input_,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
    }


def event(kind, **payload):
    return {"timestamp": WHEN, "type": "event_msg", "payload": {"type": kind, **payload}}


def snapshot(total, last=None, **payload):
    return event("token_count", info={"total_token_usage": total, "last_token_usage": total if last is None else last}, **payload)


def start(turn="turn-1"):
    return event("task_started", turn_id=turn)


def stop(turn="turn-1"):
    return event("task_complete", turn_id=turn)


def context(turn="turn-1", model="gpt-6-astra", **fields):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model, **fields}}


class UsageLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "usage.jsonl"

    def read(self, records, tail="", expected=THREAD):
        self.path.write_text("".join(json.dumps(record) + "\n" for record in records) + tail, encoding="utf-8")
        result = usage_log.read_usage_log(self.path, expected)
        for turn in result["turns"]:
            slices = turn["usageSlices"]
            self.assertTrue(all(set(item) == {"day", "model", "serviceTier", "pricingMetadataStatus", "tokens"}
                                and item["pricingMetadataStatus"] in {"known", "unknown"} for item in slices))
            if turn["tokens"] is None:
                self.assertEqual(slices, [])
                continue
            for key, amount in turn["tokens"].items():
                self.assertEqual(sum(item["tokens"][key] for item in slices), amount)
                self.assertEqual(sum(item["tokens"][key] for item in slices if item["day"] is None),
                                 turn["undatedTokens"][key])
                for day, bucket in turn["dailyUsage"].items():
                    self.assertEqual(sum(item["tokens"][key] for item in slices if item["day"] == day), bucket[key])
        return result

    def test_full_turn_multiple_calls_and_next_turn_use_deltas(self):
        result = self.read([
            META, start(), snapshot(counters()),
            snapshot(counters(160, 40, 20, 10), counters()), stop(),
            start("turn-2"), snapshot(counters(240, 60, 30, 15), counters()), stop("turn-2"),
        ])
        first, second = result["turns"]
        self.assertEqual(first["tokens"]["total"], 200)
        self.assertEqual(second["tokens"]["total"], 100)
        self.assertEqual(first["quality"], "complete")
        self.assertEqual(second["quality"], "complete")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["startedAt"], 1789344000.0)
        self.assertEqual(result["warnings"], [])
        self.assertIn("格式可能随 Codex 更新", result["sourceNote"])
        self.assertIn("仅统计该任务自身", first["note"])

    def test_missing_baseline_counts_only_last_then_new_increments(self):
        result = self.read([
            META, start(), snapshot(counters(800, 200, 100, 50), counters()),
            snapshot(counters(880, 220, 110, 55), counters()), stop(),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(turn["quality"], "partial")
        self.assertIn("基线", turn["note"])

    def test_missing_last_never_counts_thread_lifetime_total(self):
        first = snapshot(counters(800, 200, 100, 50))
        first["payload"]["info"]["last_token_usage"] = None
        result = self.read([META, start(), first, snapshot(counters(880, 220, 110, 55), counters()), stop()])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 100)
        self.assertEqual(turn["quality"], "partial")

    def test_duplicates_do_not_reopen_turns_or_add_usage(self):
        result = self.read([
            META, start(), start(), snapshot(counters()), snapshot(counters()),
            stop(), stop(), start(), snapshot(counters()),
        ])
        self.assertEqual(len(result["turns"]), 1)
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)
        self.assertEqual(result["turns"][0]["status"], "completed")

    def test_counter_reset_uses_bounded_last_and_deduplicates(self):
        reset = counters(16, 4, 2, 1)
        next_total = counters(32, 8, 4, 2)
        result = self.read([
            META, start(), snapshot(counters()), snapshot(reset), snapshot(reset),
            snapshot(next_total, reset), stop(),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 140)
        self.assertEqual(turn["quality"], "partial")
        self.assertIn("重置", turn["note"])

    def test_compaction_does_not_recount_a_repeated_snapshot(self):
        result = self.read([
            META, start(), snapshot(counters()), {"type": "compacted", "payload": {"message": "discard me"}},
            snapshot(counters()), snapshot(counters(160, 40, 20, 10), counters()), stop(),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(turn["quality"], "partial")
        self.assertIn("压缩", turn["note"])

    def test_foreign_session_is_rejected_without_content_in_exception(self):
        foreign = {"type": "session_meta", "payload": {"id": "secret-session-id"}}
        with self.assertRaises(usage_log.UsageLogError) as caught:
            self.read([foreign, start(), snapshot(counters())])
        self.assertNotIn("secret", str(caught.exception))

    def test_later_foreign_metadata_cannot_return_earlier_data(self):
        foreign = {"type": "session_meta", "payload": {"id": "other-thread"}}
        with self.assertRaises(usage_log.UsageLogError):
            self.read([META, start(), snapshot(counters()), foreign])

    def test_session_metadata_is_mandatory_and_must_precede_usage(self):
        for records in ([start()], [start(), META], []):
            with self.subTest(records=records), self.assertRaises(usage_log.UsageLogError):
                self.read(records)

    def test_invalid_counters_are_not_silently_zeroed(self):
        mutations = [
            {"input_tokens": True}, {"output_tokens": -1}, {"total_tokens": 101},
            {"cached_input_tokens": 81}, {"reasoning_output_tokens": 21}, {"cache_write_input_tokens": "0"},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invalid = counters()
                invalid.update(mutation)
                turn = self.read([META, start(), snapshot(invalid), stop()])["turns"][0]
                self.assertIsNone(turn["tokens"])
                self.assertEqual(turn["quality"], "unavailable")

    def test_null_usage_info_is_ignored_not_zero(self):
        no_usage = self.read([META, start(), event("token_count", info=None), stop()])["turns"][0]
        self.assertIsNone(no_usage["tokens"])
        self.assertEqual(no_usage["quality"], "unavailable")
        known = self.read([META, start(), snapshot(counters()), event("token_count", info=None), stop()])["turns"][0]
        self.assertEqual(known["tokens"]["total"], 100)
        self.assertEqual(known["quality"], "complete")

    def test_out_of_turn_usage_is_not_charged_to_adjacent_turns(self):
        result = self.read([
            META, snapshot(counters()), start(), snapshot(counters(160, 40, 20, 10), counters()), stop(),
            snapshot(counters(240, 60, 30, 15), counters()),
            start("turn-2"), snapshot(counters(320, 80, 40, 20), counters()), stop("turn-2"),
        ])
        self.assertEqual([turn["tokens"]["total"] for turn in result["turns"]], [100, 100])
        self.assertTrue(any("无法归属" in warning for warning in result["warnings"]))

    def test_explicit_foreign_turn_usage_is_excluded_from_next_delta(self):
        result = self.read([
            META, start(), snapshot(counters()),
            snapshot(counters(160, 40, 20, 10), counters(), turn_id="other-turn"),
            snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(turn["quality"], "partial")

    def test_other_thread_cannot_replace_the_local_cumulative_baseline(self):
        result = self.read([
            META, start(), snapshot(counters()),
            snapshot(counters(800, 200, 100, 50), thread_id="other-thread"),
            snapshot(counters(160, 40, 20, 10), counters()), stop(),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(turn["quality"], "partial")

    def test_incomplete_trailing_record_does_not_leak_or_break_read(self):
        result = self.read([META, start(), snapshot(counters())], tail='{"secret-prompt": "unfinished')
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 100)
        self.assertEqual(turn["quality"], "partial")
        self.assertNotIn("secret-prompt", json.dumps(result))
        self.assertTrue(any("尚未写完" in warning for warning in result["warnings"]))

    def test_messages_reasoning_tools_and_unknown_metadata_never_escape(self):
        secret = "PRIVATE MESSAGE BODY 5cc9be"
        result = self.read([
            {**META, "payload": {**META["payload"], "cwd": secret, "instructions": secret}},
            {"type": "response_item", "payload": {"type": "message", "content": secret}},
            {"type": "response_item", "payload": {"type": "reasoning", "text": secret}},
            event("user_message", message=secret),
            event("task_started", turn_id="turn-1", prompt=secret),
            snapshot({**counters(), "secret": secret}),
            event("task_complete", turn_id="turn-1", last_agent_message=secret),
        ])
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertEqual(set(result), {"threadId", "turns", "warnings", "sourceNote", "reading", "identityVerified", "conversationMetadata"})
        self.assertEqual(set(result["turns"][0]), {
            "id", "threadId", "startedAt", "endedAt", "status", "quality", "note", "tokens",
            "model", "serviceTier", "pricingMetadataStatus",
            "dailyUsage", "undatedTokens", "usageSlices",
        })

    def test_unknown_completion_does_not_hijack_active_turn(self):
        result = self.read([META, start(), stop("unknown-turn"), snapshot(counters()), stop()])
        known, unknown = result["turns"]
        self.assertEqual(known["tokens"]["total"], 100)
        self.assertEqual(unknown["quality"], "unavailable")
        self.assertIsNone(unknown["tokens"])

    def test_unidentified_completion_suspends_unlabelled_usage_attribution(self):
        result = self.read([
            META, start(), snapshot(counters()), event("task_complete"),
            snapshot(counters(160, 40, 20, 10), counters()),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 100)
        self.assertEqual(turn["quality"], "partial")
        self.assertEqual(turn["status"], "interrupted")

    def test_invalid_snapshot_marks_gap_and_counts_only_the_next_known_call(self):
        invalid = counters(800, 200, 100, 50)
        invalid["total_tokens"] = 999
        result = self.read([
            META, start(), snapshot(counters()), snapshot(invalid),
            snapshot(counters(880, 220, 110, 55), counters()), stop(),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(turn["quality"], "partial")

    def test_new_start_marks_missing_completion_partial(self):
        result = self.read([
            META, start(), snapshot(counters()), start("turn-2"),
            snapshot(counters(160, 40, 20, 10), counters()), stop("turn-2"),
        ])
        first, second = result["turns"]
        self.assertEqual(first["status"], "interrupted")
        self.assertEqual(first["quality"], "partial")
        self.assertEqual(second["tokens"]["total"], 100)

    def test_invalid_timestamp_cannot_return_arbitrary_text(self):
        record = start()
        record["timestamp"] = "private prompt text"
        turn = self.read([META, record, snapshot(counters())])["turns"][0]
        self.assertIsNone(turn["startedAt"])
        self.assertNotIn("private prompt", json.dumps(turn))

    def test_pricing_model_without_tier_never_implies_standard(self):
        turn = self.read([META, start(), context(), snapshot(counters()), stop()])["turns"][0]
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertIsNone(turn["serviceTier"])
        self.assertEqual(turn["pricingMetadataStatus"], "unknown")
        self.assertEqual(turn["quality"], "complete")

    def test_no_context_or_missing_model_has_unknown_pricing(self):
        for records in ([], [context(model=None, service_tier="standard")]):
            with self.subTest(records=records):
                turn = self.read([META, start(), *records, snapshot(counters()), stop()])["turns"][0]
                self.assertIsNone(turn["model"])
                self.assertEqual(turn["pricingMetadataStatus"], "unknown")

    def test_repeated_explicit_model_and_tier_are_known(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), context(service_tier="standard"),
            snapshot(counters()), stop(),
        ])["turns"][0]
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertEqual(turn["serviceTier"], "standard")
        self.assertEqual(turn["pricingMetadataStatus"], "known")

    def test_model_change_marks_mixed_without_damaging_token_quality(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            context(model="gpt-5.6-sol", service_tier="standard"),
            snapshot(counters(160, 40, 20, 10), counters()), stop(),
        ])["turns"][0]
        self.assertIsNone(turn["model"])
        self.assertEqual(turn["serviceTier"], "standard")
        self.assertEqual(turn["pricingMetadataStatus"], "mixed")
        self.assertEqual(turn["quality"], "complete")
        self.assertEqual(turn["tokens"]["total"], 200)

    def test_tier_changes_before_usage_keep_only_the_used_context(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), context(service_tier="priority"),
            snapshot(counters()), stop(),
        ])["turns"][0]
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertEqual(turn["serviceTier"], "priority")
        self.assertEqual(turn["pricingMetadataStatus"], "known")

    def test_metadata_changes_without_usage_do_not_create_mixed_history(self):
        cases = [
            (context(), context(service_tier="standard")),
            (context(service_tier="standard"), context()),
            (context(model=None, service_tier="standard"), context(service_tier="standard")),
            (context(service_tier="standard"), context(model=None, service_tier="standard")),
        ]
        for pair in cases:
            with self.subTest(pair=pair):
                turn = self.read([META, start(), *pair, snapshot(counters()), stop()])["turns"][0]
                expected = pair[-1]["payload"]
                self.assertEqual(turn["model"], expected.get("model"))
                self.assertEqual(turn["serviceTier"], expected.get("service_tier"))
                self.assertEqual(turn["pricingMetadataStatus"], "known" if expected.get("model")
                                 and expected.get("service_tier") else "unknown")
                self.assertEqual(turn["quality"], "complete")

    def test_context_before_start_is_attached_only_to_exact_turn(self):
        result = self.read([
            META, context("other-turn", model="other-model", service_tier="priority"),
            context(service_tier="standard"), start(), snapshot(counters()),
            context("unrelated-turn", model="unrelated-model", service_tier="batch"), stop(),
        ])
        self.assertEqual(len(result["turns"]), 1)
        turn = result["turns"][0]
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertEqual(turn["serviceTier"], "standard")
        self.assertEqual(turn["pricingMetadataStatus"], "known")
        self.assertNotIn("other-model", json.dumps(result))
        self.assertNotIn("unrelated-model", json.dumps(result))

    def test_pending_context_uses_latest_metadata_before_any_usage(self):
        turn = self.read([
            META, context(), context(service_tier="standard"), start(), snapshot(counters()), stop(),
        ])["turns"][0]
        self.assertEqual(turn["pricingMetadataStatus"], "known")
        self.assertEqual(turn["serviceTier"], "standard")

    def test_same_turn_model_slices_survive_switching_back(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            context(model="gpt-5.6-luna", service_tier="standard"),
            snapshot(counters(160, 40, 20, 10), counters()),
            context(service_tier="standard"), snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])["turns"][0]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turn["usageSlices"]],
                         [("gpt-6-astra", 200), ("gpt-5.6-luna", 100)])
        self.assertEqual(turn["pricingMetadataStatus"], "mixed")

    def test_late_first_context_never_backfills_unknown_usage(self):
        turn = self.read([
            META, start(), snapshot(counters()), context(model="gpt-5.6-luna", service_tier="standard"),
            snapshot(counters(160, 40, 20, 10), counters()), stop(),
        ])["turns"][0]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turn["usageSlices"]],
                         [(None, 100), ("gpt-5.6-luna", 100)])
        self.assertIsNone(turn["model"])

    def test_unused_and_completed_context_changes_cannot_relabel_history(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            context(model="gpt-5.6-luna", service_tier="priority"), snapshot(counters()), stop(),
            context(model="gpt-5.6-sol", service_tier="fast"), start(), snapshot(counters()),
        ])["turns"][0]
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertEqual(turn["serviceTier"], "standard")
        self.assertEqual(turn["pricingMetadataStatus"], "known")
        self.assertEqual(len(turn["usageSlices"]), 1)
        self.assertEqual(turn["usageSlices"][0]["tokens"]["total"], 100)

    def test_zero_usage_does_not_create_a_model_group(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters(0, 0, 0, 0)),
            context(model="gpt-5.6-luna", service_tier="standard"), snapshot(counters()), stop(),
        ])["turns"][0]
        self.assertEqual(turn["model"], "gpt-5.6-luna")
        self.assertEqual(len(turn["usageSlices"]), 1)

    def test_cross_model_delta_preserves_unconfirmed_remainder(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            context(model="gpt-5.6-luna", service_tier="standard"),
            snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])["turns"][0]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turn["usageSlices"]],
                         [("gpt-6-astra", 100), (None, 100), ("gpt-5.6-luna", 100)])
        self.assertEqual(turn["usageSlices"][1]["pricingMetadataStatus"], "unknown")
        self.assertEqual(turn["quality"], "complete")

    def test_tier_boundary_preserves_model_and_only_loses_uncertain_tier(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            context(service_tier="fast"), snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])["turns"][0]
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertEqual([(item["model"], item["serviceTier"], item["tokens"]["total"])
                          for item in turn["usageSlices"]],
                         [("gpt-6-astra", "standard", 100), ("gpt-6-astra", None, 100),
                          ("gpt-6-astra", "fast", 100)])

    def test_boundary_without_coherent_last_does_not_guess_current_model(self):
        for last in (None, counters(30, 10, 0, 5)):
            # The second last value leaves cachedInput > input in the residual.
            second = snapshot(counters(120, 30, 30, 10))
            second["payload"]["info"]["last_token_usage"] = last
            with self.subTest(last=last):
                turn = self.read([
                    META, start(), context(service_tier="standard"), snapshot(counters()),
                    context(model="gpt-5.6-luna", service_tier="standard"), second, stop(),
                ])["turns"][0]
                self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turn["usageSlices"]],
                                 [("gpt-6-astra", 100), (None, 50)])

    def test_replayed_zero_snapshot_does_not_erase_a_pending_boundary(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            context(model="gpt-5.6-luna", service_tier="standard"), snapshot(counters()),
            snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])["turns"][0]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turn["usageSlices"]],
                         [("gpt-6-astra", 100), (None, 100), ("gpt-5.6-luna", 100)])

    def test_dated_and_undated_slices_keep_all_six_counters(self):
        first, second, third = snapshot(counters()), snapshot(counters(160, 40, 20, 10), counters()), snapshot(counters(240, 60, 30, 15), counters())
        first["timestamp"] = "2026-09-14T12:00:00+00:00"
        second["timestamp"] = "2026-09-16T12:00:00+00:00"
        third.pop("timestamp")
        turn = self.read([META, start(), context(service_tier="standard"), first, second, third, stop()])["turns"][0]
        expected_days = [datetime.fromisoformat(item["timestamp"]).astimezone().date().isoformat()
                         for item in (first, second)] + [None]
        self.assertEqual([item["day"] for item in turn["usageSlices"]], expected_days)
        self.assertTrue(all(item["tokens"] == turn["undatedTokens"] for item in turn["usageSlices"]))

    def test_gap_invalidates_only_future_context_until_fresh_evidence(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            event("token_count", info="synthetic malformed metadata"),
            snapshot(counters(160, 40, 20, 10), counters()),
            context(model="gpt-5.6-luna", service_tier="standard"),
            snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])["turns"][0]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turn["usageSlices"]],
                         [("gpt-6-astra", 100), (None, 100), ("gpt-5.6-luna", 100)])
        self.assertEqual(turn["quality"], "partial")

    def test_incremental_context_does_not_mutate_earlier_exports(self):
        parser = usage_log._Reader(THREAD)
        for record in [META, start(), context(service_tier="standard"), snapshot(counters())]:
            parser.handle(record)
        before = parser.result()["turns"][0]
        parser.handle(context(model="gpt-5.6-luna", service_tier="standard"))
        parser.handle(snapshot(counters(160, 40, 20, 10), counters()))
        after = parser.result()["turns"][0]
        self.assertEqual(before["model"], "gpt-6-astra")
        self.assertEqual(len(before["usageSlices"]), 1)
        self.assertEqual(len(after["usageSlices"]), 2)
        after["usageSlices"][0]["tokens"]["total"] = 99999
        self.assertEqual(parser.result()["turns"][0]["usageSlices"][0]["tokens"]["total"], 100)

    def test_cross_turn_models_remain_independent(self):
        turns = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()), stop(),
            start("turn-2"), context("turn-2", model="gpt-5.6-luna", service_tier="standard"),
            snapshot(counters(160, 40, 20, 10), counters()), stop("turn-2"),
        ])["turns"]
        self.assertEqual([turn["model"] for turn in turns], ["gpt-6-astra", "gpt-5.6-luna"])
        self.assertEqual([turn["tokens"]["total"] for turn in turns], [100, 100])

    def test_context_before_start_covers_the_whole_first_cumulative_delta(self):
        turns = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()), stop(),
            context("turn-2", model="gpt-5.6-luna", service_tier="standard"), start("turn-2"),
            snapshot(counters(320, 80, 40, 20), counters()), stop("turn-2"),
        ])["turns"]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turns[1]["usageSlices"]],
                         [("gpt-5.6-luna", 300)])

    def test_context_after_start_does_not_claim_unobserved_earlier_calls(self):
        turns = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()), stop(),
            start("turn-2"), context("turn-2", model="gpt-5.6-luna", service_tier="standard"),
            snapshot(counters(320, 80, 40, 20), counters()), stop("turn-2"),
        ])["turns"]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turns[1]["usageSlices"]],
                         [(None, 200), ("gpt-5.6-luna", 100)])

    def test_context_only_selects_safe_bounded_metadata_fields(self):
        secret = "PRIVATE MESSAGE BODY 79812"
        invalid_context = context(model=secret, service_tier="x" * 129, prompt=secret, instructions=secret)
        wrong_id = context(turn=secret, model="gpt-5.6-sol", service_tier="standard")
        turn = self.read([META, start(), invalid_context, wrong_id, snapshot(counters()), stop()])["turns"][0]
        self.assertIsNone(turn["model"])
        self.assertIsNone(turn["serviceTier"])
        self.assertEqual(turn["pricingMetadataStatus"], "unknown")
        self.assertNotIn(secret, json.dumps(turn))
        self.assertNotIn("x" * 129, json.dumps(turn))

    def test_pending_context_limit_fails_pricing_completeness_closed(self):
        records = [META, *[
            context(f"pending-{index}", service_tier="standard")
            for index in range(usage_log.MAX_PENDING_CONTEXTS + 1)
        ], start("pending-0"), context("pending-0", service_tier="standard"), snapshot(counters()), stop("pending-0")]
        result = self.read(records)
        turn = result["turns"][0]
        self.assertEqual(turn["pricingMetadataStatus"], "unknown")
        self.assertEqual(turn["quality"], "complete")
        self.assertTrue(any("模型记录过多" in warning for warning in result["warnings"]))

    def test_symlink_leaf_is_rejected(self):
        self.path.write_text(json.dumps(META) + "\n")
        link = Path(self.temp.name) / "alias.jsonl"
        try:
            link.symlink_to(self.path)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows account has no symlink creation privilege")
            raise
        with self.assertRaises(usage_log.UsageLogError):
            usage_log.read_usage_log(link, THREAD)

    def test_nonregular_path_and_suffix_are_rejected(self):
        self.path.write_text(json.dumps(META) + "\n")
        directory = Path(self.temp.name) / "folder.jsonl"
        directory.mkdir()
        with self.assertRaises(usage_log.UsageLogError):
            usage_log.read_usage_log(directory, THREAD)
        with self.assertRaises(usage_log.UsageLogError):
            usage_log.read_usage_log(Path(self.temp.name) / "usage.txt", THREAD)


if __name__ == "__main__":
    unittest.main()
