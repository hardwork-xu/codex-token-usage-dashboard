"""Independent Credits conversion using synthetic, conserved model/day slices."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from periods import summarize_periods
from pricing import RATES, estimate_turn, standard_rates


def counters(incoming=100000, cached=90000, outgoing=5000, reasoning=4000, cache_write=0):
    return {"total": incoming + outgoing, "input": incoming, "cachedInput": cached,
            "output": outgoing, "reasoningOutput": reasoning, "cacheWriteInput": cache_write}


def fragment(model="gpt-6-astra", day="2026-10-02", tokens=None, context="short", tier=None):
    return {"day": day, "model": model, "serviceTier": tier,
            "pricingMetadataStatus": "known" if model and tier else "unknown",
            "apiContext": context, "tokens": counters() if tokens is None else tokens}


def turn(*parts, ident="synthetic-turn", quality="complete"):
    parts = parts or (fragment(),)
    total = dict.fromkeys(counters(), 0)
    daily, undated = {}, dict.fromkeys(counters(), 0)
    for part in parts:
        bucket = undated if part["day"] is None else daily.setdefault(part["day"], dict.fromkeys(counters(), 0))
        for field in total:
            total[field] += part["tokens"][field]
            bucket[field] += part["tokens"][field]
    return {"id": ident, "threadId": "synthetic-thread", "model": "gpt-6-astra",
            "quality": quality, "status": "completed", "pricingMetadataStatus": "mixed",
            "tokens": total, "dailyUsage": daily, "undatedTokens": undated,
            "usageSlices": list(parts)}


class IndependentCreditTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"pricingMode": "api", "speedMode": "standard", "currencyPerUsd": "1",
                         "usdPerCredit": "0.04", "subscriptionRenewalDay": 9}

    def estimate(self, value=None, **settings):
        return estimate_turn(turn() if value is None else value, self.settings | settings)

    def periods(self, values, **kwargs):
        return summarize_periods(values, self.settings, now=date(2026, 10, 2), **kwargs)

    def assertConserved(self, value):
        estimate = value["creditEstimate"]
        rows = [row["creditEstimate"] for row in value["models"]]
        self.assertEqual(sum(row["unpricedTokens"] for row in rows), estimate["unpricedTokens"])
        known = [Decimal(row["credits"]) for row in rows if row["credits"] is not None]
        if known:
            self.assertEqual(sum(known), Decimal(estimate["credits"]))
        elif rows:
            self.assertIsNone(estimate["credits"])

    def test_api_default_keeps_dollar_semantics_and_adds_model_credits(self):
        result = self.estimate()
        self.assertEqual(result["amount"], "0.440000")
        self.assertEqual(result["usd"], "0.440000")
        self.assertIsNone(result["credits"])
        credit = result["creditEstimate"]
        self.assertEqual((credit["credits"], credit["status"], credit["unpricedTokens"]),
                         ("11.000000", "estimated", 0))
        self.assertIsNone(credit["standardRates"])
        row = result["models"][0]
        self.assertIsNone(row["credits"])
        self.assertEqual(row["standardRates"]["unit"], "usd_per_million_tokens")
        self.assertEqual(row["creditEstimate"]["standardRates"], standard_rates("gpt-6-astra"))
        self.assertIn("不代表官方实际扣除", credit["note"])
        self.assertConserved(result)

    def test_custom_amount_and_official_credits_are_independent(self):
        result = self.estimate(pricingMode="custom", ratePerMillion="2.5")
        self.assertEqual(result["amount"], "0.262500")
        self.assertIsNone(result["usd"])
        self.assertIsNone(result["credits"])
        self.assertEqual(result["creditEstimate"]["credits"], "11.000000")
        self.assertEqual(result["models"][0]["creditEstimate"]["credits"], "11.000000")

    def test_every_known_model_uses_its_own_credit_table_in_api_and_custom(self):
        expected = {"gpt-6-astra": "1525.000000", "gpt-6.1-sol": "302.500000",
                    "gpt-6-sol": "305.000000", "gpt-6-luna": "15.250000",
                    "gpt-5.6-sol": "610.000000", "gpt-5.6-terra": "355.000000",
                    "gpt-5.6-luna": "35.500000", "gpt-5.5": "887.500000",
                    "gpt-5.4": "443.750000", "gpt-5.4-mini": "133.625000"}
        self.assertEqual(set(expected), set(RATES))
        for mode in ("api", "custom"):
            for model, credits in expected.items():
                with self.subTest(mode=mode, model=model):
                    value = turn(fragment(model, tokens=counters(2000000, 1000000, 1000000, 0)))
                    result = self.estimate(value, pricingMode=mode, ratePerMillion="1")
                    self.assertEqual(result["creditEstimate"]["credits"], credits)
                    self.assertEqual(result["models"][0]["creditEstimate"]["credits"], credits)
                    self.assertConserved(result)

    def test_currency_and_custom_conversion_cannot_change_or_hide_credits(self):
        expected = self.estimate()["creditEstimate"]
        for mode in ("api", "official", "custom"):
            for value in ("0", "777", "NaN", [], None):
                with self.subTest(mode=mode, value=value):
                    result = self.estimate(pricingMode=mode, usdPerCredit=value,
                                           currencyPerUsd=value, ratePerMillion=value)
                    self.assertEqual(result["creditEstimate"], expected)

    def test_saved_speed_affects_credits_but_not_api_standard(self):
        for speed, credits in (("standard", "11.000000"), ("fast", "22.000000"),
                               ("ultrafast", "66.000000"), ("auto", "16.500000")):
            with self.subTest(speed=speed):
                result = self.estimate(speedMode=speed)
                self.assertEqual(result["usd"], "0.440000")
                self.assertEqual(result["creditEstimate"]["credits"], credits)
        result = estimate_turn(turn(), {"pricingMode": "api"})
        self.assertEqual(result["creditEstimate"]["credits"], "11.000000")
        result = self.estimate(turn(fragment(tier="fast")), speedMode="auto")
        self.assertEqual(result["creditEstimate"]["credits"], "22.000000")

    def test_api_context_surcharges_never_enter_credit_estimates(self):
        for context, dollars in (("short", "0.440000"), ("long", "0.755000"), ("unknown", "0.597500")):
            with self.subTest(context=context):
                result = self.estimate(turn(fragment(context=context)))
                self.assertEqual(result["usd"], dollars)
                self.assertEqual(result["creditEstimate"]["credits"], "11.000000")
        result = self.estimate(turn(*(fragment(context=value) for value in ("short", "long", "unknown"))))
        self.assertEqual(result["creditEstimate"]["credits"], "33.000000")
        self.assertConserved(result)

    def test_api_can_be_complete_while_credit_coverage_is_partial(self):
        value = turn(fragment(), fragment("gpt-5.4-mini"))
        result = self.estimate(value, speedMode="auto")
        self.assertEqual((result["status"], result["unpricedTokens"]), ("estimated", 0))
        self.assertEqual(result["creditEstimate"]["status"], "partial")
        self.assertEqual(result["creditEstimate"]["credits"], "16.500000")
        self.assertEqual(result["creditEstimate"]["unpricedTokens"], 105000)
        self.assertConserved(result)
        self.settings["speedMode"] = "auto"
        period = self.periods([value])["today"]
        self.assertEqual(period["status"], "complete")
        self.assertEqual(period["creditEstimate"]["status"], "partial")
        self.assertConserved(period)

    def test_unknown_reserve_spark_and_review_models_never_become_zero(self):
        for model in (None, "gpt-reserve", "gpt-5.3-codex-spark", "synthetic-auto-review-model"):
            with self.subTest(model=model):
                value = turn(fragment(model))
                result = self.estimate(value, pricingMode="custom", ratePerMillion="1")
                self.assertEqual(result["status"], "estimated")
                credit = result["creditEstimate"]
                self.assertIsNone(credit["credits"])
                self.assertEqual((credit["status"], credit["unpricedTokens"]), ("unavailable", 105000))
                self.assertIsNone(result["models"][0]["creditEstimate"]["standardRates"])
                period = self.periods([value])["today"]
                self.assertIsNone(period["creditEstimate"]["credits"])
                self.assertEqual(period["creditEstimate"]["unpricedTokens"], 105000)
                self.assertConserved(result)
                self.assertConserved(period)

    def test_known_unknown_and_cache_write_keep_the_known_subtotal_once(self):
        value = turn(fragment(), fragment("gpt-6.1-sol"), fragment("gpt-reserve"),
                     fragment(tokens=counters(cache_write=1)))
        result = self.estimate(value)
        self.assertEqual(result["creditEstimate"]["credits"], "12.975000")
        self.assertEqual(result["creditEstimate"]["unpricedTokens"], 210000)
        self.assertEqual(result["creditEstimate"]["status"], "partial")
        by_model = {row["model"]: row["creditEstimate"] for row in result["models"]}
        self.assertEqual(by_model["gpt-6-astra"]["credits"], "11.000000")
        self.assertEqual(by_model["gpt-6-astra"]["unpricedTokens"], 105000)
        self.assertConserved(result)
        period = self.periods([value])["today"]
        self.assertEqual(period["creditEstimate"]["credits"], "12.975000")
        self.assertConserved(period)

    def test_fx_failure_does_not_make_complete_credit_period_partial(self):
        self.settings["currencyPerUsd"] = "NaN"
        period = self.periods([turn()])["today"]
        self.assertIsNone(period["amount"])
        self.assertEqual(period["creditEstimate"]["credits"], "11.000000")
        self.assertEqual(period["creditEstimate"]["status"], "estimated")
        self.assertEqual(period["models"][0]["creditEstimate"]["status"], "estimated")

    def test_days_models_and_duplicate_snapshots_conserve_period_credits(self):
        value = turn(fragment(day="2026-09-08", tokens=counters(9000, 0, 0, 0)),
                     fragment(day="2026-10-01", tokens=counters(1000, 0, 0, 0)),
                     fragment("gpt-6.1-sol", tokens=counters(2000, 0, 0, 0)),
                     fragment(tokens=counters(3000, 0, 0, 0)))
        result = self.periods([deepcopy(value), value])
        self.assertEqual(result["today"]["creditEstimate"]["credits"], "0.850000")
        self.assertEqual(result["subscription"]["creditEstimate"]["credits"], "1.100000")
        for period in (result["today"], result["subscription"]):
            self.assertEqual(period["turnCount"], 1)
            self.assertConserved(period)
        many = [turn(fragment(tokens=counters(1000, 0, 0, 0)), ident=f"synthetic-{n}") for n in range(251)]
        period = self.periods(many)["today"]
        self.assertEqual(period["creditEstimate"]["credits"], "62.750000")
        self.assertConserved(period)

    def test_empty_unknown_undated_and_partial_coverage_are_distinct(self):
        self.assertEqual(self.periods([])["today"]["creditEstimate"]["credits"], "0.000000")
        for kwargs in ({"reading_incomplete": True}, {"read_error_count": 1}):
            credit = self.periods([], **kwargs)["today"]["creditEstimate"]
            self.assertEqual(credit["status"], "unavailable")
            self.assertIsNone(credit["credits"])
        credit = self.periods([turn(fragment(day=None))])["today"]["creditEstimate"]
        self.assertIsNone(credit["credits"])
        credit = self.periods([turn(quality="partial")])["today"]["creditEstimate"]
        self.assertEqual((credit["credits"], credit["status"]), ("11.000000", "partial"))
        self.settings["subscriptionRenewalDay"] = None
        credit = self.periods([turn()])["subscription"]["creditEstimate"]
        self.assertEqual(credit["status"], "unavailable")
        self.assertIn("续订日", credit["note"])

    def test_legacy_dates_remain_visible_through_mixed_periods(self):
        value = turn(fragment("gpt-5.4"), fragment("gpt-6.1-sol"))
        result = self.estimate(value)
        self.assertTrue(result["creditEstimate"]["historical"])
        self.assertIsNone(result["creditEstimate"]["rateDate"])
        period = self.periods([value, turn(ident="another-turn")])["today"]
        self.assertTrue(period["creditEstimate"]["historical"])
        self.assertIsNone(period["creditEstimate"]["rateDate"])
        self.assertIn("历史", period["creditEstimate"]["note"])
        by_model = {row["model"]: row["creditEstimate"] for row in period["models"]}
        self.assertEqual(by_model["gpt-5.4"]["rateDate"], "2026-09-27")
        self.assertEqual(by_model["gpt-6.1-sol"]["rateDate"], "2026-09-30")
        self.assertConserved(period)

    def test_credits_round_after_model_slices_and_do_not_mutate_inputs(self):
        value = turn(*(fragment("gpt-5.6-luna", context=context, tokens=counters(1, 1, 0, 0))
                       for context in ("short", "long", "unknown")))
        before, settings_before = deepcopy(value), deepcopy(self.settings)
        with localcontext() as context:
            context.prec = 3
            result = self.estimate(value)
        self.assertEqual(result["creditEstimate"]["credits"], "0.000002")
        self.assertConserved(result)
        self.assertEqual(value, before)
        self.assertEqual(self.settings, settings_before)

    def test_invalid_model_slices_do_not_fall_back_to_top_level_astra(self):
        value = turn(fragment("gpt-6.1-sol"))
        value["usageSlices"][0]["tokens"]["total"] += 1
        result = self.estimate(value)
        self.assertIsNone(result["creditEstimate"]["credits"])
        self.assertEqual(result["creditEstimate"]["unpricedTokens"], 105000)
        self.assertIsNone(result["models"][0]["model"])
        self.assertConserved(result)


if __name__ == "__main__":
    unittest.main()
