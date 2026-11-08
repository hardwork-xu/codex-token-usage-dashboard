"""Synthetic fixed subscription boundaries preserve event-level accounting."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from periods import select_subscription_turns, summarize_periods, validated_subscription_period
from pricing import estimate_turn
from usage_log import _Turn


def counters(total, cached=0):
    return {"total": total, "input": total, "cachedInput": cached,
            "cacheWriteInput": 0, "output": 0, "reasoningOutput": 0}


def observed(rows):
    turn = _Turn("fixture-turn", "fixture-thread", 0)
    turn.status = "completed"
    for timestamp, model, tier, amount in rows:
        turn.add(amount, timestamp, metadata={"model": model, "serviceTier": tier,
                                             "pricingMetadataStatus": "known"}, api_context="short")
    return turn.export()


class SubscriptionIntervalTests(unittest.TestCase):
    def setUp(self):
        self.period = {"startAt": "2026-03-12T03:00:00+08:00",
                       "endAt": "2026-04-12T14:00:00+08:00", "timeZone": "Asia/Shanghai"}
        self.settings = {"pricingMode": "api", "speedMode": "standard", "currencyPerUsd": "1",
                         "usdPerCredit": "0.04", "subscriptionRenewalDay": 12,
                         "subscriptionPeriod": deepcopy(self.period)}
        self.start = datetime.fromisoformat(self.period["startAt"]).timestamp()
        self.end = datetime.fromisoformat(self.period["endAt"]).timestamp()

    def summarize(self, turns, **kwargs):
        return summarize_periods(turns, self.settings, now=date(2026, 4, 13), **kwargs)["subscription"]

    def test_exact_start_included_end_excluded_with_midturn_model_and_speed_switches(self):
        included = [(self.start, "gpt-6-astra", "default", counters(1_000_000, 500_000)),
                    (self.end - 0.1, "gpt-5.6-luna", "priority", counters(2_000_000))]
        all_rows = [(self.start - 0.1, "gpt-6-astra", "priority", counters(4_000_000)),
                    *included, (self.end, "gpt-6-astra", "priority", counters(8_000_000))]
        value = observed(all_rows)
        value["model"] = "gpt-6-astra"  # A final turn label must not relabel slices.
        result = self.summarize([value])
        self.assertEqual(result["tokens"], counters(3_000_000, 500_000))
        self.assertEqual(result["turnCount"], 1)
        self.assertEqual({row["model"]: row["tokens"]["total"] for row in result["models"]},
                         {"gpt-6-astra": 1_000_000, "gpt-5.6-luna": 2_000_000})
        expected = estimate_turn(observed(included), self.settings)
        self.assertEqual(result["usd"], expected["usd"])
        self.assertEqual(result["creditEstimate"]["credits"], expected["creditEstimate"]["credits"])
        selected = select_subscription_turns([value], self.settings)
        self.assertEqual({row["serviceTier"] for row in selected["turns"][0]["usageSlices"]},
                         {"default", "priority"})
        for key in ("startAt", "endAt", "timeZone"):
            self.assertEqual(result[key], self.period[key])
        self.assertEqual(result["source"], "custom")
        self.assertTrue(result["endExclusive"])
        self.assertIn("不包含结束时刻", result["note"])

    def test_fixed_period_is_stable_after_end_and_works_without_monthly_day(self):
        self.settings["subscriptionRenewalDay"] = None
        value = observed([(self.start + 10, "gpt-6-astra", "default", counters(100)),
                          (self.end + 10, "gpt-6-astra", "default", counters(1000))])
        result = summarize_periods([value], self.settings, now=date(2026, 6, 20))
        self.assertEqual(result["subscription"]["tokens"]["total"], 100)
        self.assertEqual(result["subscription"]["startAt"], self.period["startAt"])
        self.assertEqual(result["today"]["tokens"]["total"], 0)

    def test_unknown_event_time_remains_unassigned_and_does_not_erase_known_amount(self):
        value = observed([(self.start + 10, "gpt-6-astra", "default", counters(1000)),
                          (None, "gpt-5.6-luna", "priority", counters(2000))])
        result = self.summarize([value])
        self.assertEqual(result["tokens"]["total"], 1000)
        self.assertEqual(result["unassignedTokens"], 2000)
        self.assertTrue(result["partial"])
        self.assertGreater(Decimal(result["usd"]), 0)

    def test_legacy_full_local_days_preserved_boundary_day_not_guessed(self):
        # Local days are the established legacy schema, irrespective of runner TZ.
        start = datetime(2026, 3, 12).astimezone()
        end = datetime(2026, 4, 12, 14).astimezone()
        self.settings["subscriptionPeriod"] = {
            "startAt": start.astimezone(timezone.utc).isoformat(),
            "endAt": end.astimezone(timezone.utc).isoformat(), "timeZone": "UTC"}
        value = {"id": "old", "threadId": "old-thread", "tokens": counters(3300),
                 "dailyUsage": {"2026-03-12": counters(100), "2026-04-11": counters(200),
                                "2026-04-12": counters(3000)},
                 "undatedTokens": counters(0), "model": "gpt-6-astra", "quality": "complete",
                 "serviceTier": "default", "pricingMetadataStatus": "known", "apiContext": "short"}
        result = self.summarize([value])
        self.assertEqual(result["tokens"]["total"], 300)
        self.assertEqual(result["unassignedTokens"], 3000)
        self.assertTrue(result["partial"])

    def test_only_unknown_boundary_day_is_unavailable_not_zero(self):
        end = datetime(2026, 4, 12, 14).astimezone()
        self.settings["subscriptionPeriod"] = {
            "startAt": datetime(2026, 4, 12, 10).astimezone().isoformat(),
            "endAt": end.isoformat(), "timeZone": "UTC"}
        value = {"id": "old", "threadId": "old-thread", "tokens": counters(100),
                 "dailyUsage": {"2026-04-12": counters(100)}, "undatedTokens": counters(0),
                 "model": "gpt-6-astra", "quality": "complete"}
        result = self.summarize([value])
        self.assertIsNone(result["tokens"])
        self.assertIsNone(result["usd"])
        self.assertEqual(result["unassignedTokens"], 100)

    def test_precomputed_selection_supports_display_only_turns(self):
        value = observed([(self.start + 10, "gpt-6-astra", "default", counters(100)),
                          (self.end + 10, "gpt-6-astra", "priority", counters(900))])
        selection = select_subscription_turns([value], self.settings)
        stripped = {key: val for key, val in value.items() if key != "usageSegments"}
        self.assertEqual(self.summarize([value]), self.summarize([stripped], subscription_selection=selection))

    def test_duplicate_snapshots_and_inputs_remain_lossless(self):
        first = observed([(self.start + 10, "gpt-6-astra", "default", counters(100))])
        latest = observed([(self.start + 10, "gpt-6-astra", "default", counters(200))])
        original = deepcopy([first, latest])
        selected = select_subscription_turns([first, latest], self.settings)
        self.assertEqual(sum(row["tokens"]["total"] for row in selected["turns"]), 200)
        selected["turns"][0]["usageSlices"][0]["tokens"]["total"] = 9999
        self.assertEqual([first, latest], original)

    def test_date_speed_declaration_is_preserved_in_exact_slices(self):
        self.settings["speedOverrideWindows"] = {"2026-03-12": {
            "startAt": int(self.start), "endAt": int(self.start) + 86400}}
        self.settings["speedOverrides"] = {"2026-03-12": "fast"}
        value = observed([(self.start + 10, "gpt-6-astra", None, counters(100))])
        selected = select_subscription_turns([value], self.settings)
        self.assertEqual(selected["turns"][0]["usageSlices"][0]["speedOverrideDay"], "2026-03-12")

    def test_invalid_segments_do_not_inflate_day_fallback(self):
        value = observed([(self.start + 86400, "gpt-6-astra", "default", counters(100))])
        value["usageSegments"][0]["tokens"] = counters(200)
        result = self.summarize([value])
        self.assertEqual(result["tokens"]["total"], 100)
        self.assertTrue(result["partial"])

    def test_legacy_missing_context_uses_same_projection_rule(self):
        value = observed([(self.start + 86400, "gpt-6-astra", "default", counters(100))])
        expected = estimate_turn(value, self.settings)
        value["usageSegments"][0].pop("apiContext")
        result = self.summarize([value])
        self.assertEqual(result["usd"], expected["usd"])
        self.assertEqual(result["apiContextUncertainTokens"], expected["apiContextUncertainTokens"])

    def test_removing_custom_interval_returns_unchanged_monthly_path(self):
        value = observed([(self.start + 86400, "gpt-6-astra", "default", counters(100))])
        self.settings["subscriptionPeriod"] = None
        result = self.summarize([value])
        self.assertEqual((result["startDate"], result["endDate"]), ("2026-04-12", "2026-05-12"))
        self.assertNotIn("startAt", result)
        self.assertIsNone(select_subscription_turns([value], self.settings))

    def test_invalid_period_is_rejected_instead_of_silently_monthly(self):
        self.assertEqual(validated_subscription_period(self.period), self.period)
        self.assertIsNone(validated_subscription_period(None))
        for period in ({}, True, {**self.period, "extra": 1},
                       {**self.period, "startAt": "2026-03-12T03:00:00"},
                       {**self.period, "endAt": self.period["startAt"]},
                       {**self.period, "endAt": "2028-04-12T14:00:00+08:00"},
                       {**self.period, "timeZone": "not/a-real-zone"}):
            with self.subTest(period=period), self.assertRaises(ValueError):
                validated_subscription_period(period)


if __name__ == "__main__":
    unittest.main()
