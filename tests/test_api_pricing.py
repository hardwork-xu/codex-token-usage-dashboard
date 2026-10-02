"""Synthetic API replacement-cost checks; no account, logs or API calls."""
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from pricing import (API_LONG_CONTEXT_MODELS, API_RATE_DATE, API_RATES,
                     API_SOURCE_URL, estimate_turn, standard_rates, validated_slices)


def counters(incoming=100000, cached=90000, outgoing=5000, reasoning=4000):
    return {"total": incoming + outgoing, "input": incoming, "cachedInput": cached,
            "cacheWriteInput": 0, "output": outgoing, "reasoningOutput": reasoning}


def turn(model="gpt-6-astra", context="short", tokens=None):
    return {"id": "synthetic-turn", "threadId": "synthetic-thread", "model": model,
            "serviceTier": "default", "pricingMetadataStatus": "known", "apiContext": context,
            "quality": "complete", "status": "completed", "tokens": counters() if tokens is None else tokens}


def sliced_turn(*parts):
    result = turn()
    result["tokens"] = {key: sum(part["tokens"][key] for part in parts) for key in counters()}
    result["dailyUsage"] = {"2026-09-27": dict(result["tokens"])}
    result["undatedTokens"] = dict.fromkeys(counters(), 0)
    result["usageSlices"] = [{key: part[key] for key in
                              ("model", "serviceTier", "pricingMetadataStatus", "apiContext", "tokens")} |
                             {"day": "2026-09-27"} for part in parts]
    return result


class ApiPricingTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"pricingMode": "api", "currencyPerUsd": "1"}

    def estimate(self, record=None, **settings):
        return estimate_turn(record if record is not None else turn(), self.settings | settings)

    def test_short_example_separates_cached_and_uncached_inputs(self):
        result = self.estimate()
        self.assertEqual(result["usd"], "0.440000")
        self.assertEqual(result["amount"], "0.440000")
        self.assertIsNone(result["credits"])
        self.assertEqual(result["estimateBasis"], "api_standard")
        self.assertEqual(result["apiContextUncertainTokens"], 0)
        self.assertEqual(result["apiLongContextTokens"], 0)
        self.assertEqual(result["breakdown"]["uncachedInput"]["tokens"], 10000)
        self.assertEqual(result["breakdown"]["cachedInput"]["tokens"], 90000)
        self.assertEqual(result["sourceUrl"], "https://developers.openai.com/api/docs/models/gpt-6-astra")
        self.assertEqual(result["rateDate"], API_RATE_DATE)

    def test_each_published_model_has_an_independent_api_rate(self):
        # One million tokens in each billed category; context is supplied evidence,
        # not inferred from the aggregate counts (which can span many requests).
        expected = {"gpt-6.1-sol": "12.100000", "gpt-6-astra": "61.000000", "gpt-6-sol": "12.200000",
                    "gpt-6-luna": "0.610000", "gpt-5.6-sol": "24.400000",
                    "gpt-5.6-terra": "14.200000", "gpt-5.6-luna": "1.420000",
                    "gpt-5.5": "35.500000", "gpt-5.4": "17.750000",
                    "gpt-5.4-mini": "5.325000"}
        self.assertEqual(set(expected), set(API_RATES))
        for model, usd in expected.items():
            with self.subTest(model=model):
                result = self.estimate(turn(model, tokens=counters(2000000, 1000000, 1000000, 0)))
                self.assertEqual(result["usd"], usd)
                self.assertIsNone(result["credits"])

    def test_model_slices_override_top_level_model_and_sum_once(self):
        record = sliced_turn(*(turn(model) for model in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna")))
        result = self.estimate(record)
        self.assertEqual(result["usd"], "0.532400")
        self.assertEqual(result["estimateBasis"], "api_slices")
        self.assertEqual(result["sourceUrl"], API_SOURCE_URL)
        self.assertEqual(sum(Decimal(row["amount"]) for row in result["models"]), Decimal(result["amount"]))
        self.assertEqual(sum(row["tokens"]["total"] for row in result["models"]), record["tokens"]["total"])

    def test_api_ignores_credit_value_and_codex_speed_settings(self):
        expected = self.estimate()
        for value in ("0", "999", "NaN", [], None):
            for speed in ("standard", "fast", "ultrafast", "auto", "invalid"):
                with self.subTest(value=value, speed=speed):
                    actual = self.estimate(usdPerCredit=value, speedMode=speed)
                    # Independent credits intentionally follow the saved speed;
                    # the selected API comparison remains Standard in every case.
                    actual.pop("creditEstimate")
                    reference = deepcopy(expected)
                    reference.pop("creditEstimate")
                    for row in actual["models"] + reference["models"]:
                        row.pop("creditEstimate")
                    self.assertEqual(actual, reference)

    def test_recorded_service_tier_does_not_change_standard_comparison(self):
        for tier in (None, "fast", "ultrafast", "priority", "default"):
            record = turn()
            record.update(serviceTier=tier, pricingMetadataStatus="unknown")
            self.assertEqual(self.estimate(record)["usd"], "0.440000")

    def test_reasoning_subset_and_effort_do_not_add_cost(self):
        record = turn()
        expected = self.estimate(record)["usd"]
        record["tokens"]["reasoningOutput"] = 0
        record["reasoningEffort"] = "ultra"
        self.assertEqual(self.estimate(record)["usd"], expected)

    def test_new_sol_has_lower_cached_rate_without_changing_old_sol(self):
        tokens = counters(1000000, 1000000, 0, 0)
        self.assertEqual(self.estimate(turn("gpt-6.1-sol", tokens=tokens))["usd"], "0.100000")
        self.assertEqual(self.estimate(turn("gpt-6-sol", tokens=tokens))["usd"], "0.200000")
        record = sliced_turn(turn("gpt-6.1-sol"), turn("gpt-6-sol"), turn())
        result = self.estimate(record)
        self.assertEqual(result["usd"], "0.607000")
        self.assertEqual({row["model"]: row["usd"] for row in result["models"]},
                         {"gpt-6.1-sol": "0.079000", "gpt-6-sol": "0.088000", "gpt-6-astra": "0.440000"})
        self.assertEqual(sum(Decimal(row["usd"]) for row in result["models"]), Decimal(result["usd"]))

    def test_new_sol_long_context_and_unknown_midpoint_keep_new_cache_rate(self):
        for context, expected in (("short", "0.079000"), ("long", "0.133000"), ("unknown", "0.106000")):
            with self.subTest(context=context):
                result = self.estimate(turn("gpt-6.1-sol", context))
                self.assertEqual(result["usd"], expected)
                self.assertIsNone(result["credits"])
        short = turn("gpt-6.1-sol", "short", counters(272000, 0, 0, 0))
        long = turn("gpt-6.1-sol", "long", counters(272001, 0, 0, 0))
        self.assertEqual(self.estimate(short)["usd"], "0.544000")
        self.assertEqual(self.estimate(long)["usd"], "1.088004")
        rate = standard_rates("gpt-6.1-sol", "api")
        self.assertEqual(rate["longContextThreshold"], 272000)
        self.assertEqual(rate["longContextScope"], "request")
        self.assertEqual(rate["sourceUrl"], "https://developers.openai.com/api/docs/models/gpt-6.1-sol")
        self.assertEqual(rate["rateDate"], "2026-09-30")

    def test_new_sol_api_remains_standard_independent_of_credit_scenarios(self):
        record = turn("gpt-6.1-sol")
        for speed in ("standard", "fast", "ultrafast", "auto"):
            with self.subTest(speed=speed):
                result = self.estimate(record, usdPerCredit="NaN", speedMode=speed)
                self.assertEqual(result["usd"], "0.079000")
                self.assertIsNone(result["credits"])
                self.assertEqual(result["billingBasis"], "api_standard")

    def test_long_context_multiplies_full_input_cache_and_output(self):
        result = self.estimate(turn(context="long"))
        self.assertEqual(result["usd"], "0.755000")
        self.assertEqual(result["estimateBasis"], "api_long")
        self.assertEqual(result["breakdown"]["cachedInput"]["contextMultiplier"], "2")
        self.assertEqual(result["breakdown"]["output"]["contextMultiplier"], "1.5")
        self.assertEqual(result["apiContextUncertainTokens"], 0)
        self.assertEqual(result["apiLongContextTokens"], 105000)
        self.assertEqual(result["models"][0]["apiLongContextTokens"], 105000)

    def test_threshold_uses_explicit_context_evidence_not_turn_total(self):
        # Upstream marks >272,000 request input as long, not >=272,000.
        short = turn(tokens=counters(272000, 0, 0, 0))
        long = turn(context="long", tokens=counters(272001, 0, 0, 0))
        self.assertEqual(self.estimate(short)["usd"], "2.720000")
        self.assertEqual(self.estimate(long)["usd"], "5.440020")
        # An aggregate of many short requests must not inherit a long surcharge.
        aggregate = turn(tokens=counters(600000, 0, 0, 0))
        self.assertEqual(self.estimate(aggregate)["usd"], "6.000000")

    def test_unknown_context_uses_unrounded_midpoint_and_exposes_coverage(self):
        record = turn(context="unknown")
        result = self.estimate(record)
        self.assertEqual(result["usd"], "0.597500")
        self.assertEqual(result["estimateBasis"], "api_context_midpoint")
        self.assertEqual(result["apiContextUncertainTokens"], 105000)
        self.assertEqual(result["models"][0]["apiContextUncertainTokens"], 105000)
        self.assertEqual(result["apiLongContextTokens"], 0)
        self.assertIn("中点", result["note"])
        for key in ("creditsMax", "usdMax", "amountMax"):
            self.assertIsNone(result[key])

    def test_missing_or_invalid_context_stays_unknown(self):
        for context in (None, [], {}, "not-evidence"):
            with self.subTest(context=context):
                self.assertEqual(self.estimate(turn(context=context))["usd"], "0.597500")

    def test_slices_preserve_different_contexts_before_grouping(self):
        record = sliced_turn(turn(context="short"), turn(context="long"), turn(context="unknown"))
        self.assertEqual([item["apiContext"] for item in validated_slices(record)], ["short", "long", "unknown"])
        result = self.estimate(record)
        self.assertEqual(result["usd"], "1.792500")
        self.assertEqual(len(result["models"]), 1)
        self.assertEqual(result["apiContextUncertainTokens"], 105000)
        self.assertEqual(result["models"][0]["apiContextUncertainTokens"], 105000)
        self.assertEqual(result["apiLongContextTokens"], 105000)
        self.assertEqual(result["models"][0]["apiLongContextTokens"], 105000)
        self.assertIn("中点", result["note"])

    def test_mini_does_not_inherit_undocumented_long_surcharge(self):
        self.assertEqual(API_LONG_CONTEXT_MODELS, frozenset(API_RATES) - {"gpt-5.4-mini"})
        values = [self.estimate(turn("gpt-5.4-mini", context)) for context in ("short", "long", "unknown")]
        self.assertEqual({result["usd"] for result in values}, {"0.036750"})
        self.assertTrue(all(result["apiContextUncertainTokens"] == 0 for result in values))

    def test_spark_unknown_model_and_alias_never_use_astra_rate(self):
        for model in ("gpt-5.3-codex-spark", None, "unknown", "gpt-6-astra-latest"):
            with self.subTest(model=model):
                result = self.estimate(turn(model))
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["amount"])
                self.assertEqual(result["unpricedTokens"], 105000)
                self.assertEqual(result["models"][0]["model"], model)

    def test_unpriced_slices_stay_visible_without_inflating_money(self):
        result = self.estimate(sliced_turn(turn(), turn("gpt-5.3-codex-spark"), turn(None)))
        self.assertEqual(result["usd"], "0.440000")
        self.assertEqual(result["unpricedTokens"], 210000)
        self.assertEqual(result["status"], "partial")

    def test_cache_write_mapping_remains_unavailable(self):
        record = turn()
        record["tokens"]["cacheWriteInput"] = 1
        result = self.estimate(record)
        self.assertIsNone(result["amount"])
        self.assertIn("映射尚未核实", result["note"])

    def test_new_sol_published_cache_write_price_does_not_invent_log_mapping(self):
        unsupported = turn("gpt-6.1-sol")
        unsupported["tokens"]["cacheWriteInput"] = 1
        self.assertIsNone(self.estimate(unsupported)["amount"])
        record = sliced_turn(turn("gpt-6.1-sol"), unsupported)
        result = self.estimate(record)
        self.assertEqual(result["usd"], "0.079000")
        self.assertEqual(result["unpricedTokens"], 105000)
        self.assertEqual(result["status"], "partial")

    def test_fx_uses_unrounded_usd_and_not_credit_conversion(self):
        record = turn("gpt-6-luna", "unknown", counters(1, 1, 0, 0))
        result = self.estimate(record, currencyPerUsd="1000000")
        self.assertEqual(result["usd"], "0.000000")
        self.assertEqual(result["amount"], "0.015000")

    def test_model_group_rounds_once_after_context_slices(self):
        # A large FX makes premature USD rounding observable: unrounded USD
        # totals 0.0000015, while separately rounded USD would total 0.000002.
        parts = [turn("gpt-5.4-mini", context, counters(1, 0, 0, 0)) for context in ("short", "long")]
        result = self.estimate(sliced_turn(*parts), currencyPerUsd="1000")
        self.assertEqual(result["usd"], "0.000002")
        self.assertEqual(result["amount"], "0.001500")

    def test_api_rate_metadata_is_dollars_and_model_specific(self):
        for model in API_RATES:
            rate = standard_rates(model, "api")
            self.assertEqual(rate["unit"], "usd_per_million_tokens")
            self.assertEqual(rate["sourceUrl"], "https://developers.openai.com/api/docs/models/" + model)
            self.assertEqual(rate["rateDate"], "2026-09-30")
            self.assertFalse(rate["historical"])
            self.assertEqual(rate["billingBasis"], "api_standard")
        self.assertEqual(standard_rates("gpt-5.4-mini", "api")["output"], "4.5")
        self.assertEqual(standard_rates("gpt-5.4-mini")["output"], "113")
        self.assertEqual(self.estimate()["models"][0]["standardRates"]["unit"], "usd_per_million_tokens")
        self.assertEqual(standard_rates("gpt-6-astra", "api")["longContextThreshold"], 272000)
        self.assertEqual(standard_rates("gpt-6-astra", "api")["longContextCachedInputMultiplier"], "2")
        self.assertEqual(standard_rates("gpt-5.4", "api")["longContextScope"], "session")
        self.assertNotIn("longContextThreshold", standard_rates("gpt-5.4-mini", "api"))

    def test_partial_and_running_status_are_visible(self):
        record = sliced_turn(turn(), turn("gpt-6-sol", "unknown"))
        record.update(quality="partial", status="running")
        result = self.estimate(record)
        self.assertEqual(result["status"], "partial")
        self.assertIn("截至目前", result["note"])

    def test_global_decimal_precision_and_inputs_are_unchanged(self):
        record = sliced_turn(turn(), turn("gpt-6-sol", "unknown"))
        before = deepcopy(record)
        expected = self.estimate(record)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(self.estimate(record), expected)
        self.assertEqual(record, before)

    def test_invalid_fx_and_slice_conservation_fail_closed(self):
        self.assertIsNone(self.estimate(currencyPerUsd="NaN")["amount"])
        record = sliced_turn(turn())
        record["usageSlices"][0]["tokens"]["total"] += 1
        result = self.estimate(record)
        self.assertIsNone(result["amount"])
        self.assertIsNone(result["models"][0]["model"])


if __name__ == "__main__":
    unittest.main()
