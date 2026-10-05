"""Speed declarations never rewrite other days, known tiers, or API prices."""
from datetime import date
from decimal import Decimal
import unittest

from test_credit_estimates import counters, fragment, turn
from pricing import estimate_turn
from periods import summarize_periods


class SpeedAccountingTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"pricingMode": "api", "speedMode": "standard", "currencyPerUsd": "1",
                         "usdPerCredit": "0.04", "subscriptionRenewalDay": 1,
                         "speedOverrides": {"2026-10-05": "fast"}}

    def test_midnight_same_model_keeps_declared_day_separate(self):
        value = turn(fragment(day="2026-10-04"), fragment(day="2026-10-05"))
        result = estimate_turn(value, self.settings)
        self.assertEqual(result["usd"], "0.880000")
        self.assertEqual(result["creditEstimate"]["credits"], "33.000000")
        rows = result["creditEstimate"]["speedBreakdown"]
        self.assertCountEqual(rows, [{"speed": "standard", "source": "fallback", "tokens": 105000},
                                     {"speed": "fast", "source": "date_override", "tokens": 105000}])
        periods = summarize_periods([value], self.settings, now=date(2026, 10, 5))
        self.assertEqual(periods["today"]["creditEstimate"]["credits"], "22.000000")
        self.assertEqual(periods["subscription"]["creditEstimate"]["credits"], "33.000000")
        self.assertEqual(periods["today"]["creditEstimate"]["speedBreakdown"],
                         [{"speed": "fast", "source": "date_override", "tokens": 105000}])

    def test_known_tier_always_wins_over_legacy_default_and_date_confirmation(self):
        for tier, credits in (("standard", "11.000000"), ("fast", "22.000000"), ("ultrafast", "66.000000")):
            for fallback in ("auto", "standard", "fast", "ultrafast"):
                with self.subTest(tier=tier, fallback=fallback):
                    result = estimate_turn(turn(fragment(day="2026-10-05", tier=tier)),
                                           self.settings | {"speedMode": fallback})
                    self.assertEqual(result["creditEstimate"]["credits"], credits)
                    self.assertEqual(result["usd"], "0.440000")
                    self.assertEqual(result["creditEstimate"]["speedBreakdown"],
                                     [{"speed": tier, "source": "recorded", "tokens": 105000}])

    def test_undated_usage_never_inherits_turn_start_date(self):
        value = turn(fragment(day=None)) | {"startedAt": 1791150000}
        result = estimate_turn(value, self.settings)
        self.assertEqual(result["creditEstimate"]["credits"], "11.000000")
        self.assertEqual(result["creditEstimate"]["speedBreakdown"][0]["source"], "fallback")

    def test_legacy_turn_top_level_day_is_not_event_date_evidence(self):
        value = {"day": "2026-10-05", "tokens": counters(), "model": "gpt-6-astra",
                 "quality": "complete", "pricingMetadataStatus": "unknown"}
        result = estimate_turn(value, self.settings)["creditEstimate"]
        self.assertEqual(result["credits"], "11.000000")
        self.assertEqual(result["speedBreakdown"][0]["source"], "fallback")

    def test_model_totals_and_speed_coverage_conserve_tokens(self):
        value = turn(fragment(day="2026-10-04"), fragment(day="2026-10-05"),
                     fragment("gpt-6.1-sol", day="2026-10-05", tier="standard"),
                     fragment("gpt-reserve", day="2026-10-05"))
        result = estimate_turn(value, self.settings)
        credit = result["creditEstimate"]
        self.assertEqual(sum(row["tokens"] for row in credit["speedBreakdown"]), value["tokens"]["total"])
        self.assertEqual(sum(Decimal(row["creditEstimate"]["credits"] or "0") for row in result["models"]),
                         Decimal(credit["credits"]))
        self.assertEqual(credit["unpricedTokens"], 105000)
        for model in result["models"]:
            self.assertEqual(sum(row["tokens"] for row in model["creditEstimate"]["speedBreakdown"]),
                             model["tokens"]["total"])

    def test_different_confirmed_days_and_future_do_not_share_speed(self):
        value = turn(*(fragment(day=day) for day in ("2026-10-04", "2026-10-05", "2026-10-06")))
        settings = self.settings | {"speedOverrides": {"2026-10-04": "ultrafast", "2026-10-05": "fast"}}
        self.assertEqual(estimate_turn(value, settings)["creditEstimate"]["credits"], "99.000000")

    def test_incomplete_tier_evidence_uses_date_confirmation(self):
        part = fragment(day="2026-10-05", tier="ultrafast") | {"pricingMetadataStatus": "unknown"}
        credit = estimate_turn(turn(part), self.settings)["creditEstimate"]
        self.assertEqual(credit["credits"], "22.000000")
        self.assertEqual(credit["speedBreakdown"][0]["source"], "date_override")

    def test_invalid_slices_cannot_apply_unverified_day_or_speed(self):
        value = turn(fragment(day="2026-10-05", tier="fast")) | {"day": "2026-10-05"}
        value["tokens"]["total"] += 10
        value["tokens"]["input"] += 10
        result = estimate_turn(value, self.settings)
        self.assertIsNone(result["creditEstimate"]["credits"])
        self.assertEqual(result["creditEstimate"]["speedBreakdown"],
                         [{"speed": "standard", "source": "fallback", "tokens": 105010}])


if __name__ == "__main__":
    unittest.main()
