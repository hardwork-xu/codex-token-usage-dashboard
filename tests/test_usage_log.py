"""Synthetic tests: no real transcripts, credentials, or configuration are read."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


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


def fork_meta(boundary=7, parent="parent-fixture", **fields):
    return {"type": "session_meta", "payload": {
        "id": THREAD, "forked_from_id": parent, "parent_thread_id": parent,
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}},
        "history_mode": "paginated", "subagent_history_start_ordinal": boundary, **fields}}


def numbered(records):
    return [{**record, "ordinal": index} for index, record in enumerate(records)]


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
            self.assertTrue(all(set(item) == {"day", "speedOverrideDay", "model", "serviceTier", "pricingMetadataStatus", "apiContext", "tokens"}
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

    def test_api_context_tracks_individual_calls_not_large_turn_total(self):
        result = self.read([META, start(), context(),
                            snapshot(counters(272000, 20)),
                            snapshot(counters(544001, 40, 20, 10), counters(272001, 20)), stop()])
        slices = result["turns"][0]["usageSlices"]
        self.assertEqual({row["apiContext"]: row["tokens"]["input"] for row in slices},
                         {"short": 272000, "long": 272001})

    def test_missing_calls_do_not_invent_long_context_from_aggregate(self):
        result = self.read([META, start(), context(), snapshot(counters(100, 20)),
                            snapshot(counters(700100, 60, 30, 15), counters(100000, 20)), stop()])
        slices = result["turns"][0]["usageSlices"]
        self.assertEqual({row["apiContext"]: row["tokens"]["input"] for row in slices},
                         {"short": 100100, "unknown": 600000})

    def test_partial_call_delta_does_not_prove_a_short_request(self):
        result = self.read([META, start(), context(), snapshot(counters(300000, 20)),
                            snapshot(counters(300000, 30), counters(300000, 30)), stop()])
        slices = result["turns"][0]["usageSlices"]
        self.assertEqual({row["apiContext"]: row["tokens"]["total"] for row in slices},
                         {"long": 300020, "unknown": 10})

    def test_legacy_full_session_threshold_is_not_inferred_from_short_call(self):
        result = self.read([META, start(), context(model="gpt-5.5"), snapshot(counters()), stop()])
        self.assertEqual(result["turns"][0]["usageSlices"][0]["apiContext"], "unknown")

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
        self.assertEqual(turn["quality"], "complete")
        self.assertNotIn("压缩", turn["note"])

    def test_compaction_changed_counter_retains_partial_and_only_last_call(self):
        result = self.read([
            META, start(), context(service_tier="fast"), snapshot(counters()),
            {"type": "compacted", "payload": {}},
            snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])
        turn = result["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(turn["quality"], "partial")
        self.assertIsNone(turn["usageSlices"][-1]["model"])

    def test_compaction_waiting_for_proof_stays_partial_without_mutating_reader(self):
        reader = usage_log._Reader(THREAD)
        for record in [META, start(), context(), snapshot(counters()), {"type": "compacted"}]:
            reader.handle(record)
        self.assertEqual(reader.result()["turns"][0]["quality"], "partial")
        reader.handle(snapshot(counters()))
        self.assertEqual(reader.result()["turns"][0]["quality"], "complete")
        self.assertEqual(reader.result()["turns"][0]["model"], "gpt-6-astra")

    def test_compaction_stop_without_duplicate_counter_stays_partial(self):
        turn = self.read([META, start(), snapshot(counters()), {"type": "compacted"}, stop()])["turns"][0]
        self.assertEqual(turn["quality"], "partial")

    def test_compaction_and_unknown_gap_cannot_restore_previous_context(self):
        turn = self.read([META, start(), context(), snapshot(counters()),
                          {"type": "compacted"}, None, snapshot(counters()),
                          snapshot(counters(160, 40, 20, 10), counters()), stop()])["turns"][0]
        self.assertEqual(turn["quality"], "partial")
        self.assertIsNone(turn["usageSlices"][-1]["model"])

    def test_compaction_failed_proof_preserves_new_explicit_context(self):
        turn = self.read([META, start(), context(), snapshot(counters()),
                          {"type": "compacted"}, context(model="gpt-6-sol", service_tier="fast"),
                          snapshot(counters(240, 60, 30, 15), counters()), stop()])["turns"][0]
        self.assertEqual(turn["quality"], "partial")
        self.assertEqual(turn["usageSlices"][-1]["model"], "gpt-6-sol")
        self.assertEqual(turn["usageSlices"][-1]["serviceTier"], "fast")

    def test_same_turn_model_and_speed_switches_conserve_prices_and_days(self):
        from decimal import Decimal
        from pricing import estimate_turn
        from periods import summarize_periods
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters(100000, 10000, 20000, 2000)),
            context(model="gpt-6-sol", service_tier="fast"),
            snapshot(counters(200000, 20000, 40000, 4000), counters(100000, 10000, 20000, 2000)),
            context(service_tier="standard"),
            snapshot(counters(300000, 30000, 60000, 6000), counters(100000, 10000, 20000, 2000)), stop(),
        ])["turns"][0]
        settings = {"pricingMode": "api", "speedMode": "ultrafast", "currencyPerUsd": "1"}
        price = estimate_turn(turn, settings)
        # Astra twice: API 2 * (0.8 + 0.02 + 0.5), Credits 2 * 33.
        # Sol Fast once: API 0.16 + 0.004 + 0.1, Credits 6.6 * 2.
        self.assertEqual(Decimal(price["usd"]), Decimal("2.904"))
        self.assertEqual(Decimal(price["creditEstimate"]["credits"]), Decimal("79.2"))
        self.assertEqual(sum(row["tokens"]["total"] for row in turn["usageSlices"]), 330000)
        self.assertEqual({(row["model"], row["serviceTier"]): row["tokens"]["total"] for row in turn["usageSlices"]},
                         {("gpt-6-astra", "standard"): 220000, ("gpt-6-sol", "fast"): 110000})
        summaries = summarize_periods([turn], settings, now=datetime.fromtimestamp(turn["endedAt"]).date())
        self.assertEqual(summaries["today"]["tokens"]["total"], 330000)
        self.assertEqual(summaries["today"]["usd"], price["usd"])
        self.assertEqual(summaries["today"]["creditEstimate"]["credits"], price["creditEstimate"]["credits"])

    def test_verified_duplicate_closes_model_interval_with_or_without_compaction(self):
        for compact in ([], [{"type": "compacted"}]):
            with self.subTest(compact=bool(compact)):
                turn = self.read([
                    META, start(), context(service_tier="standard"), snapshot(counters()),
                    *compact, context(model="gpt-6-sol", service_tier="fast"), snapshot(counters()),
                    snapshot(counters(240, 60, 30, 15), counters()), stop(),
                ])["turns"][0]
                self.assertEqual(turn["quality"], "complete")
                self.assertEqual([(x["model"], x["serviceTier"], x["tokens"]["total"]) for x in turn["usageSlices"]],
                                 [("gpt-6-astra", "standard", 100), ("gpt-6-sol", "fast", 200)])

    def test_bound_speed_day_uses_utc_window_and_survives_local_timezone_change(self):
        from decimal import Decimal
        from zoneinfo import ZoneInfo
        from pricing import estimate_turn
        begin = int(datetime.fromisoformat("2026-10-04T21:00:00+00:00").timestamp())
        end = begin + 86400
        windows = {"2026-10-05": {"startAt": begin, "endAt": end, "timeZone": "Africa/Cairo"}}
        records = [META, start(), context()]
        for index, timestamp in enumerate((begin - 1, begin, end - 1, end), 1):
            records.append({**snapshot(counters(index * 100, 0, 0, 0), counters(100, 0, 0, 0)),
                            "timestamp": timestamp})
        records.append(stop())
        self.path.write_text("".join(json.dumps(row) + "\n" for row in records))
        settings = {"pricingMode": "api", "currencyPerUsd": "1", "speedMode": "standard",
                    "speedOverrides": {"2026-10-05": "fast"}, "speedOverrideWindows": windows}
        prices, dates = [], []
        for zone in ("Africa/Cairo", "Asia/Shanghai"):
            with mock.patch.object(usage_log, "_local_day", lambda value: datetime.fromtimestamp(value, ZoneInfo(zone)).date().isoformat() if value is not None else None):
                turn = usage_log.UsageLogReader(THREAD, override_windows=windows).read(self.path)["turns"][0]
            bound = sum(row["tokens"]["total"] for row in turn["usageSlices"] if row["speedOverrideDay"] == "2026-10-05")
            self.assertEqual(bound, 200)
            self.assertEqual(sum(row["tokens"]["total"] for row in turn["usageSlices"] if row["speedOverrideDay"] is None), 200)
            price = estimate_turn(turn, settings)
            prices.append(price["creditEstimate"]["credits"])
            dates.append(turn["dailyUsage"])
            self.assertEqual(Decimal(price["usd"]), Decimal("0.004"))
        self.assertNotEqual(dates[0], dates[1])
        self.assertEqual(prices, ["0.150000", "0.150000"])

    def test_speed_windows_reject_bad_types_and_overlaps(self):
        invalid = [False, [], {"2026-10-05": {"startAt": True, "endAt": 10}},
                   {"2026-10-05": {"startAt": 20, "endAt": 10}},
                   {"not-a-date": {"startAt": 0, "endAt": 10}},
                   {"2026-10-05": {"startAt": 0, "endAt": 10},
                    "2026-10-06": {"startAt": 9, "endAt": 20}}]
        for windows in invalid:
            with self.subTest(windows=windows), self.assertRaises(ValueError):
                usage_log.UsageLogReader(THREAD, override_windows=windows)

    def test_large_verified_response_keeps_context_and_all_usage(self):
        with mock.patch.object(usage_log, "MAX_LINE_BYTES", 1024):
            turn = self.read([
                META, start(), context(service_tier="standard"), snapshot(counters()),
                {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "private" * 1000}},
                snapshot(counters(160, 40, 20, 10), counters()), stop(),
            ])["turns"][0]
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(turn["quality"], "complete")
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertEqual(turn["serviceTier"], "standard")
        self.assertNotIn("private", json.dumps(turn))

    def test_large_response_cannot_hide_following_model_or_speed_switch(self):
        with mock.patch.object(usage_log, "MAX_LINE_BYTES", 1024):
            turn = self.read([
                META, start(), context(service_tier="standard"), snapshot(counters()),
                {"type": "response_item", "payload": {"output": "x" * 8000}},
                context(model="gpt-6-sol", service_tier="fast"),
                snapshot(counters(160, 40, 20, 10), counters()), stop(),
            ])["turns"][0]
        self.assertEqual([(x["model"], x["serviceTier"], x["tokens"]["total"]) for x in turn["usageSlices"]],
                         [("gpt-6-astra", "standard", 100), ("gpt-6-sol", "fast", 100)])

    def test_large_turn_context_stays_a_gap_and_cannot_restore_old_model(self):
        with mock.patch.object(usage_log, "MAX_LINE_BYTES", 1024):
            turn = self.read([META, start(), context(), snapshot(counters()),
                              context(model="gpt-6-sol", private="x" * 6000),
                              snapshot(counters(160, 40, 20, 10), counters()), stop()])["turns"][0]
        self.assertEqual(turn["quality"], "partial")
        self.assertIsNone(turn["usageSlices"][-1]["model"])

    def test_large_compaction_requires_counter_proof(self):
        with mock.patch.object(usage_log, "MAX_LINE_BYTES", 1024):
            turn = self.read([META, start(), context(), snapshot(counters()),
                              {"type": "compacted", "payload": {"message": "x" * 6000}},
                              snapshot(counters()), snapshot(counters(160, 40, 20, 10), counters()), stop()])["turns"][0]
        self.assertEqual(turn["quality"], "complete")
        self.assertEqual(turn["model"], "gpt-6-astra")
        self.assertEqual(turn["tokens"]["total"], 200)

    def test_large_envelope_validates_utf8_escapes_and_keeps_no_payload(self):
        value = {"payload": {"content": ["私人文本é\\x\n" * 100, {"nested": [None, True, 3.2e4]}]},
                 "ordinal": 42, "type": "response_item"}
        raw = (json.dumps(value, ensure_ascii=False) + "\n").encode()
        probe = usage_log._LargeRecordEnvelope()
        for index in range(0, len(raw), 7):
            probe.feed(raw[index:index + 7], final=index + 7 >= len(raw))
        self.assertEqual(probe.result(), {"type": "response_item", "ordinal": 42})
        self.assertNotIn("私人文本", repr(probe.__dict__))

    def test_large_envelope_rejects_duplicate_forged_or_malformed_metadata(self):
        cases = [
            b'{"type":"response_item","type":"turn_context","payload":{}}\n',
            b'{"type":"turn_context","type":"response_item","payload":{}}\n',
            b'{"type":"response_item","ordinal":4,"ordinal":5}\n',
            b'{"type":"response_item","payload":{"x":1,}}\n',
            b'{"type":"response_item","payload":[1,]}\n',
            b'{"type":"response_item","payload":"bad\\q"}\n',
            b'{"type":"response_item","payload":"bad\xff"}\n',
            b'{"type":"response_item","ordinal":true}\n',
            b'{"type":"response_item","ordinal":1.5}\n',
            b'{"type":"response_item","payload":01}\n',
            b'{"type":"response_item"} {"type":"turn_context"}\n',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                probe = usage_log._LargeRecordEnvelope()
                for index in range(0, len(raw), 3):
                    probe.feed(raw[index:index + 3], final=index + 3 >= len(raw))
                self.assertIsNone(probe.result())

    def test_large_session_header_cannot_bypass_identity_verification(self):
        for thread in (THREAD, "foreign-thread"):
            with self.subTest(thread=thread), mock.patch.object(usage_log, "MAX_LINE_BYTES", 1024):
                # Its complete envelope proves this is identity metadata, but
                # its payload is deliberately not retained or used to grant trust.
                with self.assertRaises(usage_log.UsageLogError):
                    self.read([META, start(), snapshot(counters()),
                               {"type": "session_meta", "payload": {"id": thread, "private": "x" * 6000}},
                               snapshot(counters(160, 40, 20, 10), counters()), stop()])

    def test_large_envelope_fork_ordinals_still_require_monotonicity(self):
        records = numbered([fork_meta(boundary=1), start(), context(), snapshot(counters()),
                            {"type": "response_item", "payload": {"output": "x" * 6000}},
                            snapshot(counters(160, 40, 20, 10), counters()), stop()])
        with mock.patch.object(usage_log, "MAX_LINE_BYTES", 1024):
            turn = self.read(records)["turns"][0]
            self.assertEqual(turn["tokens"]["total"], 200)
            self.assertEqual(turn["quality"], "complete")
            records[4]["ordinal"] = 1
            with self.assertRaises(usage_log.UsageLogError):
                self.read(records)

    def test_foreign_session_is_rejected_without_content_in_exception(self):
        foreign = {"type": "session_meta", "payload": {"id": "secret-session-id"}}
        with self.assertRaises(usage_log.UsageLogError) as caught:
            self.read([foreign, start(), snapshot(counters())])
        self.assertNotIn("secret", str(caught.exception))

    def test_later_foreign_metadata_cannot_return_earlier_data(self):
        foreign = {"type": "session_meta", "payload": {"id": "other-thread"}}
        with self.assertRaises(usage_log.UsageLogError):
            self.read([META, start(), snapshot(counters()), foreign])

    def fork_records(self):
        return numbered([
            fork_meta(), {"type": "session_meta", "payload": {"id": "parent-fixture"}},
            start("parent-turn"), context("parent-turn", model="gpt-5.6-luna"),
            snapshot(counters(8000, 2000)), stop("parent-turn"),
            {"type": "compacted", "payload": {"message": "inherited private text"}},
            start(), context(), snapshot(counters()), stop(),
        ])

    def test_explicit_fork_boundary_excludes_all_parent_usage_and_context(self):
        result = self.read(self.fork_records())
        self.assertEqual(len(result["turns"]), 1)
        turn = result["turns"][0]
        self.assertEqual((turn["id"], turn["model"], turn["tokens"]["total"]),
                         ("turn-1", "gpt-6-astra", 100))
        self.assertEqual(turn["quality"], "complete")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["conversationMetadata"]["parentThreadId"], "parent-fixture")
        self.assertNotIn("inherited private text", json.dumps(result))

    def test_fork_boundary_uses_explicit_ordinals_not_blank_or_malformed_line_count(self):
        records = self.fork_records()
        lines = [json.dumps(record) for record in records]
        lines.insert(4, "")
        lines.insert(6, "{invalid inherited record")
        self.path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
        result = usage_log.read_usage_log(self.path, THREAD)
        self.assertEqual([turn["tokens"]["total"] for turn in result["turns"]], [100])
        self.assertTrue(any("格式异常" in note for note in result["warnings"]))

    def test_fork_boundary_survives_incremental_reads_without_exposing_parent_turns(self):
        records = self.fork_records()
        self.path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        reader = usage_log.UsageLogReader(THREAD)
        with mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 2):
            first = reader.read(self.path)
            self.assertEqual(first["turns"], [])
            self.assertTrue(first["identityVerified"])
            self.assertFalse(first["reading"]["complete"])
            for _ in range(10):
                result = reader.read(self.path)
                self.assertNotIn("parent-turn", [turn["id"] for turn in result["turns"]])
                if result["reading"]["complete"]:
                    break
        self.assertTrue(result["reading"]["complete"])
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)

    def test_inherited_only_file_waits_for_child_history_and_handles_partial_append(self):
        records = self.fork_records()
        self.path.write_text("".join(json.dumps(record) + "\n" for record in records[:7]), encoding="utf-8")
        reader = usage_log.UsageLogReader(THREAD)
        result = reader.read(self.path)
        self.assertEqual(result["turns"], [])
        self.assertTrue(any("尚未读到自身记录" in note for note in result["warnings"]))
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(records[7]))
        result = reader.read(self.path)
        self.assertFalse(result["reading"]["complete"])
        self.assertEqual(result["turns"], [])
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\n" + "".join(json.dumps(record) + "\n" for record in records[8:]))
        result = reader.read(self.path)
        self.assertTrue(result["reading"]["complete"])
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)

    def test_nested_fork_accepts_only_declared_ancestor_chain_before_boundary(self):
        records = numbered([
            fork_meta(boundary=6),
            {"type": "session_meta", "payload": {"id": "parent-fixture", "forked_from_id": "grandparent"}},
            {"type": "session_meta", "payload": {"id": "grandparent"}},
            start("grandparent-turn"), snapshot(counters(8000, 2000)), stop("grandparent-turn"),
            start(), context(), snapshot(counters()), stop(),
        ])
        result = self.read(records)
        self.assertEqual([(turn["id"], turn["tokens"]["total"]) for turn in result["turns"]], [("turn-1", 100)])
        records[2]["payload"]["id"] = "unrelated-thread"
        with self.assertRaises(usage_log.UsageLogError):
            self.read(records)

    def test_foreign_header_after_fork_boundary_still_poisons_cached_results(self):
        records = self.fork_records()
        self.path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        reader = usage_log.UsageLogReader(THREAD)
        self.assertEqual(reader.read(self.path)["turns"][0]["tokens"]["total"], 100)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"ordinal": len(records), "type": "session_meta",
                                     "payload": {"id": "parent-fixture"}}) + "\n")
        for _ in range(2):
            with self.assertRaises(usage_log.UsageLogError):
                reader.read(self.path)

    def test_invalid_fork_metadata_cannot_disable_session_identity_checks(self):
        mutations = [{"subagent_history_start_ordinal": value} for value in (True, -1, 1.5, "7", 2**64)]
        mutations += [{"history_mode": "legacy"}, {"parent_thread_id": None},
                      {"forked_from_id": "different-parent"}, {"source": "cli"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(usage_log.UsageLogError):
                records = self.fork_records()
                records[0]["payload"].update(mutation)
                self.read(records)

    def test_missing_boolean_or_nonmonotonic_fork_ordinals_are_rejected(self):
        for index, ordinal in ((0, None), (0, True), (2, None), (2, True), (2, 1), (8, 7), (9, -1)):
            records = self.fork_records()
            if ordinal is None:
                records[index].pop("ordinal")
            else:
                records[index]["ordinal"] = ordinal
            with self.subTest(index=index, ordinal=ordinal), self.assertRaises(usage_log.UsageLogError):
                self.read(records)

    def test_older_fork_without_copied_parent_header_still_excludes_parent_counters(self):
        records = self.fork_records()
        records[1] = {"ordinal": 1, "type": "response_item", "payload": {"content": "not metadata"}}
        result = self.read(records)
        self.assertEqual([(turn["id"], turn["tokens"]["total"]) for turn in result["turns"]], [("turn-1", 100)])
        self.assertEqual(result["warnings"], [])

    def test_optional_copied_parent_header_cannot_contain_an_unrelated_identity(self):
        records = self.fork_records()
        records[1]["payload"]["id"] = "undeclared-parent"
        with self.assertRaises(usage_log.UsageLogError):
            self.read(records)

    def test_cyclic_fork_ancestry_is_rejected(self):
        for ancestor in (THREAD, "parent-fixture"):
            records = self.fork_records()
            records[1]["payload"]["forked_from_id"] = ancestor
            with self.subTest(ancestor=ancestor), self.assertRaises(usage_log.UsageLogError):
                self.read(records)

    def test_child_cumulative_counter_never_uses_inherited_parent_as_its_baseline(self):
        records = self.fork_records()
        records[9] = {**snapshot(counters(8080, 2020, 20, 10), counters()), "ordinal": 9}
        result = self.read(records)
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)
        self.assertEqual(result["turns"][0]["quality"], "partial")

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

    def test_verified_zero_snapshot_closes_a_pending_boundary(self):
        turn = self.read([
            META, start(), context(service_tier="standard"), snapshot(counters()),
            context(model="gpt-5.6-luna", service_tier="standard"), snapshot(counters()),
            snapshot(counters(240, 60, 30, 15), counters()), stop(),
        ])["turns"][0]
        self.assertEqual([(item["model"], item["tokens"]["total"]) for item in turn["usageSlices"]],
                         [("gpt-6-astra", 100), ("gpt-5.6-luna", 200)])

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
