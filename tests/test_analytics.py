"""Synthetic local analytics: exact counters, shared pricing and bounded charts."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analytics import build_analytics, MAX_MODEL_ROWS, MAX_TOP_CONVERSATIONS
from periods import summarize_periods
import meter


NOW = date(2026, 11, 7)
SETTINGS = {"pricingMode": "api", "currencyPerUsd": "1", "subscriptionRenewalDay": 9}
FIELDS = ("total", "input", "cachedInput", "cacheWriteInput", "output", "reasoningOutput")


def counts(incoming=100000, cached=90000, outgoing=5000, write=0):
    return dict(total=incoming + outgoing, input=incoming, cachedInput=cached,
                cacheWriteInput=write, output=outgoing, reasoningOutput=outgoing // 2)


def fragment(day="2026-11-07", model="gpt-6-astra", *, tier="default", context="short", tokens=None):
    return dict(day=day, model=model, serviceTier=tier, pricingMetadataStatus="known",
                apiContext=context, tokens=counts() if tokens is None else tokens)


def record(*fragments, ident="turn-1", thread="thread-1"):
    values = fragments or (fragment(),)
    totals = dict.fromkeys(FIELDS, 0)
    daily = {}
    undated = dict(totals)
    for item in values:
        bucket = undated if item["day"] is None else daily.setdefault(item["day"], dict.fromkeys(FIELDS, 0))
        for key in FIELDS:
            totals[key] += item["tokens"][key]
            bucket[key] += item["tokens"][key]
    return dict(id=ident, threadId=thread, model="gpt-6-astra", serviceTier="default",
                pricingMetadataStatus="known", quality="complete", status="completed",
                apiContext="short", tokens=totals, dailyUsage=daily, undatedTokens=undated,
                usageSlices=list(values))


class AnalyticsTests(unittest.TestCase):
    def build(self, turns=(), settings=None, **kwargs):
        return build_analytics(list(turns), SETTINGS if settings is None else settings,
                               now=kwargs.pop("now", NOW), **kwargs)

    def test_scopes_and_same_model_cost_do_not_use_account_quota(self):
        before = record(fragment("2026-10-31"), ident="before-week")
        result = self.build([before, record()])
        self.assertEqual(result["scope"], "local_registered_logs")
        self.assertEqual(result["billingBasis"], "api_standard")
        self.assertEqual(result["periods"]["week"]["startDate"], "2026-11-01")
        self.assertEqual(result["periods"]["month"]["startDate"], "2026-10-09")
        self.assertEqual(result["periods"]["subscription"]["endDate"], "2026-11-09")
        self.assertEqual(result["periods"]["week"]["usd"], "0.440000")
        self.assertEqual(result["periods"]["month"]["usd"], "0.880000")
        self.assertEqual(len(result["periods"]["month"]["daily"]), 30)
        self.assertNotIn("quota", result)

    def test_cache_is_subset_and_reasoning_not_charged_twice(self):
        value = self.build([record()])["periods"]["today"]
        self.assertEqual(value["tokens"]["total"], 105000)
        self.assertEqual(value["cacheHitRate"], .9)
        self.assertEqual(value["costBreakdown"], {
            "uncachedInputUsd": "0.100000", "cachedInputUsd": "0.090000", "outputUsd": "0.250000"})
        self.assertEqual(sum(Decimal(n) for n in value["costBreakdown"].values()), Decimal(value["usd"]))

    def test_mixed_models_tiers_and_context_match_period_estimator(self):
        turns = [record(fragment(model="gpt-6-astra", context="long"),
                        fragment(model="gpt-6-luna", tier="priority"),
                        fragment("2026-11-06", "gpt-6-astra", tier="priority", context="unknown")),
                 record(fragment(model="gpt-5.3-codex-spark"), ident="spark"),
                 record(fragment(model=None), ident="unknown")]
        expected = summarize_periods(turns, SETTINGS, now=NOW)
        actual = self.build(turns)["periods"]
        for name in ("today", "subscription"):
            self.assertEqual(actual[name]["tokens"], expected[name]["tokens"])
            self.assertEqual(actual[name]["usd"], expected[name]["usd"])
            self.assertEqual(actual[name]["unpricedTokens"], expected[name]["unpricedTokens"])
            self.assertTrue(actual[name]["partial"])
            for key in FIELDS:
                self.assertEqual(sum(row["tokens"][key] for row in actual[name]["models"]), actual[name]["tokens"][key])
            self.assertEqual(sum(Decimal(row["usd"]) for row in actual[name]["models"] if row["usd"]),
                             Decimal(actual[name]["usd"]))

    def test_currency_credit_and_speed_preferences_do_not_change_api_usd(self):
        values = [record(fragment(tier="priority"))]
        before = self.build(values)
        after = self.build(values, {**SETTINGS, "pricingMode": "custom", "currencyPerUsd": "789",
                                   "ratePerMillion": "999999", "speedMode": "ultrafast", "usdPerCredit": "20"})
        self.assertEqual(before, after)

    def test_duplicate_turn_snapshots_count_once_but_other_threads_count(self):
        first = record(fragment(tokens=counts(100, 0, 0)))
        last = record(fragment(tokens=counts(200, 0, 0)))
        other = record(fragment(tokens=counts(300, 0, 0)), thread="thread-2")
        value = self.build([first, last, deepcopy(last), other])["periods"]["today"]
        self.assertEqual(value["tokens"]["total"], 500)
        self.assertEqual(value["turnCount"], 2)

    def test_complete_empty_and_unknown_empty_are_distinct(self):
        value = self.build()["periods"]["today"]
        self.assertEqual(value["tokens"]["total"], 0)
        self.assertEqual(value["usd"], "0.000000")
        for kwargs in ({"reading_incomplete": True}, {"read_error_count": 1}):
            value = self.build(**kwargs)["periods"]["today"]
            self.assertIsNone(value["tokens"])
            self.assertIsNone(value["usd"])
            self.assertTrue(value["partial"])
        self.assertIsNone(self.build([{"tokens": None}])["periods"]["today"]["tokens"])

    def test_unpriced_does_not_become_zero(self):
        value = self.build([record(fragment(model="gpt-5.3-codex-spark"))])["periods"]["today"]
        self.assertIsNone(value["usd"])
        self.assertEqual(value["tokens"]["total"], 105000)
        self.assertEqual(value["unpricedTokens"], 105000)

    def test_undated_never_inferred_from_started_at(self):
        row = record(fragment(None))
        row["startedAt"] = datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp()
        result = self.build([row])
        self.assertEqual(result["unassignedTokens"], 105000)
        self.assertIsNone(result["periods"]["today"]["tokens"])
        self.assertTrue(result["periods"]["today"]["partial"])

    def test_no_input_has_no_cache_percentage(self):
        value = self.build([record(fragment(tokens=counts(0, 0, 10)))])["periods"]["today"]
        self.assertIsNone(value["cacheHitRate"])

    def test_invalid_model_slices_keep_counters_and_unknown_price(self):
        row = record()
        row["usageSlices"][0]["tokens"] = counts(200000)
        value = self.build([row])["periods"]["today"]
        self.assertEqual(value["tokens"]["total"], 105000)
        self.assertIsNone(value["usd"])
        self.assertEqual(value["models"][0]["model"], None)

    def test_legacy_daily_buckets_keep_known_model(self):
        row = record()
        row.pop("usageSlices")
        value = self.build([row])["periods"]["today"]
        self.assertEqual(value["usd"], "0.440000")

    def test_cache_write_mapping_does_not_hide_other_priceable_slices(self):
        row = record(fragment(tokens=counts(write=10)), fragment(model="gpt-6-luna"))
        value = self.build([row])["periods"]["today"]
        self.assertEqual(value["unpricedTokens"], 105000)
        self.assertEqual(value["usd"], "0.004400")

    def test_model_cap_conserves_all_counters_and_keeps_unknown(self):
        rows = [record(fragment(model=f"synthetic-{i}"), ident=f"turn-{i}") for i in range(50)]
        rows.append(record(fragment(model=None), ident="unknown"))
        value = self.build(rows)["periods"]["today"]
        self.assertLessEqual(len(value["models"]), MAX_MODEL_ROWS)
        self.assertTrue(any(row.get("groupedOther") for row in value["models"]))
        self.assertTrue(any(row["model"] is None for row in value["models"]))
        for key in FIELDS:
            self.assertEqual(sum(row["tokens"][key] for row in value["models"]), value["tokens"][key])

    def test_full_history_not_display_cap_and_only_ranking_thread_identity(self):
        rows = [record(ident=f"private-id-{i}") for i in range(300)]
        value = self.build(rows)
        self.assertEqual(value["periods"]["today"]["turnCount"], 300)
        self.assertEqual(value["periods"]["today"]["tokens"]["total"], 31500000)
        self.assertNotIn("private-id", json.dumps(value))
        self.assertEqual(value["periods"]["today"]["topConversations"][0]["threadId"], "thread-1")
        for period in value["periods"].values():
            period.pop("topConversations")
        self.assertNotIn("thread-1", json.dumps(value))

    def test_daily_model_stacks_conserve_mixed_tokens_and_cost(self):
        rows = [record(fragment("2026-11-06", "gpt-6-astra"),
                       fragment("2026-11-07", "gpt-6-luna"),
                       fragment("2026-11-07", None))]
        period = self.build(rows)["periods"]["week"]
        for day in period["daily"]:
            for key in FIELDS:
                self.assertEqual(sum(row["tokens"][key] for row in day["models"]), day["tokens"][key])
            self.assertEqual(sum((Decimal(row["usd"]) for row in day["models"] if row["usd"] is not None), Decimal(0)),
                             Decimal(day["usd"]))
        self.assertEqual({row["model"] for row in period["daily"][-1]["models"]}, {"gpt-6-luna", None})
        self.assertTrue(period["daily"][-1]["partial"])

    def test_daily_models_remain_bounded_without_dropping_unknown(self):
        rows = [record(fragment(model=f"synthetic-{i}"), ident=str(i)) for i in range(40)]
        rows.append(record(fragment(model=None), ident="unknown"))
        day = self.build(rows)["periods"]["today"]["daily"][0]
        self.assertEqual(len(day["models"]), MAX_MODEL_ROWS)
        self.assertTrue(any(row["model"] is None for row in day["models"]))
        self.assertEqual(sum(row["tokens"]["total"] for row in day["models"]), day["tokens"]["total"])

    def test_ranking_groups_thread_turns_and_matches_period_amount(self):
        rows = [record(fragment(model="gpt-6-astra"), thread="thread-a", ident="1"),
                record(fragment(model="gpt-6-luna"), thread="thread-a", ident="2"),
                record(fragment(model="gpt-6-astra"), thread="thread-b", ident="1")]
        period = self.build(rows + [deepcopy(rows[-1])])["periods"]["today"]
        ranking = period["topConversations"]
        self.assertEqual([row["threadId"] for row in ranking], ["thread-a", "thread-b"])
        self.assertEqual(ranking[0]["turnCount"], 2)
        self.assertEqual(ranking[0]["usd"], "0.444400")
        self.assertEqual(sum(Decimal(row["usd"]) for row in ranking), Decimal(period["usd"]))
        self.assertEqual(sum(row["tokens"]["total"] for row in ranking), period["tokens"]["total"])

    def test_ranking_unknown_amount_uses_tokens_and_stable_thread_tie(self):
        rows = [record(fragment(model=None, tokens=counts(n, 0, 0)), thread=name)
                for name, n in (("thread-b", 100), ("thread-a", 100), ("thread-c", 200))]
        ranking = self.build(rows)["periods"]["today"]["topConversations"]
        self.assertEqual([row["threadId"] for row in ranking], ["thread-c", "thread-a", "thread-b"])
        self.assertTrue(all(row["usd"] is None and row["partial"] for row in ranking))
        self.assertEqual(ranking[0]["unpricedTokens"], 200)

    def test_ranking_is_bounded_but_period_totals_include_all_threads(self):
        rows = [record(thread=f"thread-{i:02d}") for i in range(20)]
        period = self.build(rows)["periods"]["today"]
        self.assertEqual(period["topConversationsCount"], 20)
        self.assertEqual(len(period["topConversations"]), MAX_TOP_CONVERSATIONS)
        self.assertEqual([row["threadId"] for row in period["topConversations"]],
                         [f"thread-{i:02d}" for i in range(MAX_TOP_CONVERSATIONS)])
        self.assertEqual(period["usd"], "8.800000")

    def test_ranking_has_no_chat_text_paths_or_account_metadata(self):
        row = record()
        row.update(title="private-title", prompt="private-prompt", path="private-path", account="private-account")
        output = json.dumps(self.build([row]))
        for secret in ("private-title", "private-prompt", "private-path", "private-account"):
            self.assertNotIn(secret, output)

    def test_missing_renewal_and_clamped_renewal(self):
        value = self.build(settings={})["periods"]["subscription"]
        self.assertEqual(value["status"], "needs_configuration")
        self.assertEqual(value["daily"], [])
        value = self.build(settings={"subscriptionRenewalDay": 31}, now=date(2026, 3, 1))["periods"]["subscription"]
        self.assertEqual((value["startDate"], value["endDate"]), ("2026-02-28", "2026-03-31"))

    def test_micro_rounding_matches_existing_period_totals(self):
        rows = [record(*(fragment("2026-11-07", "gpt-6-luna", tokens=counts(i, 0, i % 7))
                         for i in range(1, 50)), ident=str(n)) for n in range(5)]
        value = self.build(rows)["periods"]["today"]
        expected = summarize_periods(rows, SETTINGS, now=NOW)["today"]
        self.assertEqual(value["usd"], expected["usd"])
        self.assertEqual(sum(Decimal(n) for n in value["costBreakdown"].values()), Decimal(value["usd"]))


class AnalyticsIntegrationTests(unittest.TestCase):
    def test_snapshot_passes_complete_history_before_visible_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            subject = meter.Meter(Path(folder))
            rows = [record(ident=str(i)) for i in range(251)]
            meter.write_json(Path(folder) / "registry.json", {"thread-1": {"path": "synthetic.jsonl"}})
            with mock.patch.object(meter.UsageLogReader, "read", return_value={
                    "turns": rows, "reading": {"complete": True}}), \
                    mock.patch.object(meter, "build_analytics", wraps=build_analytics) as analytics:
                result = subject.snapshot()
            self.assertEqual(len(analytics.call_args.args[0]), 251)
            self.assertEqual(len(result["turns"]), 200)
            self.assertEqual(result["analytics"]["scope"], "local_registered_logs")

    def test_slow_collection_heartbeat_records_publication_time(self):
        # No sleeping and no real logs: advancing the clock during save models
        # a long parse/archive pass that would otherwise look stale immediately.
        from test_headless_collection import records, THREAD
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "synthetic.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in records()) + "\n")
            meter.write_json(root / "registry.json", {THREAD: {"path": str(path)}})
            subject = meter.Meter(root)
            original = subject.archive.save
            clock = [1000.0]
            def slow_save(*args, **kwargs):
                clock[0] = 1100.0
                return original(*args, **kwargs)
            with mock.patch.object(meter.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(subject.archive, "save", side_effect=slow_save):
                result = subject.collect_usage()
            self.assertEqual(result["updatedAt"], 1100.0)
            self.assertGreaterEqual(result["updatedAt"], result["lastSavedAt"])
            self.assertEqual(result["pendingTasks"], 0)


if __name__ == "__main__":
    unittest.main()
