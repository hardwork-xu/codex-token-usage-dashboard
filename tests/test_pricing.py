"""Dated official-credit examples and fail-closed synthetic pricing cases."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from pricing import (estimate_turn, standard_rates, SOURCE_URL, SPEED_SOURCE_URL, RATE_DATE,
                     RATES, API_RATES, HISTORICAL_RATE_DATE, HISTORICAL_RATE_SNAPSHOTS)


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.turn = {
            "model": "gpt-6-astra", "serviceTier": None,
            "pricingMetadataStatus": "unknown", "quality": "complete", "status": "completed",
            "tokens": {"input": 100000, "cachedInput": 90000, "output": 5000,
                       "reasoningOutput": 4000, "cacheWriteInput": 0, "total": 105000},
        }
        self.settings = {"pricingMode": "official", "speedMode": "standard",
                         "usdPerCredit": "0.04", "currencyPerUsd": "1",
                         "currencyName": "美元", "currencySymbol": "$", "ratePerMillion": None}

    def estimate(self, *, turn=None, settings=None):
        return estimate_turn(turn if turn is not None else self.turn, settings if settings is not None else self.settings)

    def assertUnavailable(self, result):
        self.assertEqual(result["status"], "unavailable")
        for key in ("credits", "creditsMax", "usd", "usdMax", "amount", "amountMax", "breakdown", "estimateBasis"):
            self.assertIsNone(result[key], key)
        self.assertTrue(result["note"])

    def test_astra_worked_example_standard(self):
        result = self.estimate()
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["credits"], "11.000000")
        self.assertEqual(result["usd"], "0.440000")
        self.assertEqual(result["amount"], "0.440000")
        self.assertIsNone(result["creditsMax"])
        self.assertEqual(result["sourceUrl"], SOURCE_URL)
        self.assertEqual(result["rateDate"], RATE_DATE)
        self.assertEqual(result["estimateBasis"], "standard")
        self.assertIn("Standard（非加速）", result["note"])
        self.assertIn("不代表订阅实际扣款", result["note"])
        self.assertEqual(result["breakdown"]["uncachedInput"]["tokens"], 10000)

    def test_unknown_speed_is_one_midpoint_estimate(self):
        self.settings["speedMode"] = "auto"
        result = self.estimate()
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimateBasis"], "midpoint")
        self.assertEqual(result["credits"], "16.500000")
        self.assertEqual(result["usd"], "0.660000")
        self.assertEqual(result["amount"], "0.660000")
        self.assertIn("Standard 与 Fast 场景的中点", result["note"])
        self.assertIn("不含 Ultrafast", result["note"])
        self.assertNotIn("区间", result["note"])
        for key in ("creditsMax", "usdMax", "amountMax"):
            self.assertIsNone(result[key])

    def test_default_and_priority_are_not_inferred_as_actual_speed(self):
        self.settings["speedMode"] = "auto"
        self.turn["pricingMetadataStatus"] = "known"
        for tier in (None, "default", "priority", "standard", "other"):
            with self.subTest(tier=tier):
                self.turn["serviceTier"] = tier
                result = self.estimate()
                self.assertEqual(result["status"], "estimated")
                self.assertEqual(result["estimateBasis"], "midpoint")

    def test_explicit_recorded_fast_is_point_estimate(self):
        self.settings["speedMode"] = "auto"
        self.turn.update(serviceTier="fast", pricingMetadataStatus="known")
        result = self.estimate()
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["credits"], "22.000000")
        self.assertEqual(result["estimateBasis"], "fast")
        self.assertIsNone(result["creditsMax"])
        self.assertIn("记录中的 Fast", result["note"])

    def test_incomplete_fast_metadata_does_not_claim_actual_fast(self):
        self.settings["speedMode"] = "auto"
        self.turn.update(serviceTier="fast", pricingMetadataStatus="unknown")
        self.assertEqual(self.estimate()["estimateBasis"], "midpoint")

    def test_explicit_speed_setting_is_labeled_scenario(self):
        self.settings["speedMode"] = "fast"
        result = self.estimate()
        self.assertEqual(result["credits"], "22.000000")
        self.assertIn("用户选择", result["note"])

    def test_purchased_fast_rates_are_not_included_quota_multipliers(self):
        for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                      "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"):
            with self.subTest(model=model):
                self.turn["model"] = model
                self.settings["speedMode"] = "standard"
                baseline = Decimal(self.estimate()["credits"])
                self.settings["speedMode"] = "fast"
                result = self.estimate()
                self.assertEqual(Decimal(result["credits"]), baseline * 2)
                self.assertEqual(result["billingBasis"], "purchased_credits")
                self.assertEqual(result["breakdown"]["multiplier"], "2")
                self.assertIn("不用于推算订阅内含额度", result["note"])

    def test_ultrafast_requires_astra_and_explicit_evidence_or_scenario(self):
        self.settings["speedMode"] = "ultrafast"
        result = self.estimate()
        self.assertEqual(result["credits"], "66.000000")
        self.assertEqual(result["estimateBasis"], "ultrafast")
        self.assertIn("用户选择", result["note"])
        self.assertEqual(result["breakdown"]["multiplier"], "6")
        self.settings["speedMode"] = "auto"
        self.turn.update(serviceTier="ultrafast", pricingMetadataStatus="known")
        self.assertEqual(self.estimate()["credits"], "66.000000")
        self.assertIn("记录中的 Ultrafast", self.estimate()["note"])
        self.turn["pricingMetadataStatus"] = "unknown"
        self.assertEqual(self.estimate()["credits"], "16.500000")
        for model in ("gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.4-mini"):
            self.turn.update(model=model, pricingMetadataStatus="known")
            for speed in ("auto", "ultrafast"):
                with self.subTest(model=model, speed=speed):
                    self.settings["speedMode"] = speed
                    self.assertUnavailable(self.estimate())

    def test_new_sol_cached_credit_rate_does_not_relabel_previous_sol(self):
        self.turn["tokens"].update(input=1000000, cachedInput=1000000, output=0,
                                   total=1000000, reasoningOutput=0)
        self.turn["model"] = "gpt-6.1-sol"
        self.assertEqual(self.estimate()["credits"], "2.500000")
        self.turn["model"] = "gpt-6-sol"
        self.assertEqual(self.estimate()["credits"], "5.000000")

    def test_historical_credit_rows_keep_their_own_dates_and_warning(self):
        for model in ("gpt-5.4", "gpt-5.4-mini"):
            with self.subTest(model=model):
                self.turn["model"] = model
                result = self.estimate()
                self.assertEqual(result["rateDate"], "2026-09-27")
                self.assertTrue(result["historical"])
                self.assertIn("不能视为现行报价", result["note"])
                self.assertEqual(result["models"][0]["standardRates"], standard_rates(model))
                self.assertTrue(standard_rates(model)["historical"])
                self.assertEqual(standard_rates(model)["billingBasis"], "purchased_credits")
        self.turn["model"] = "gpt-6.1-sol"
        self.assertEqual(self.estimate()["rateDate"], "2026-09-30")
        self.assertFalse(self.estimate()["historical"])

    def test_mixed_current_and_legacy_credits_do_not_claim_one_current_date(self):
        self.turn["usageSlices"] = [
            {"day": "2026-09-30", "model": model, "serviceTier": None,
             "pricingMetadataStatus": "unknown", "tokens": deepcopy(self.turn["tokens"])}
            for model in ("gpt-6.1-sol", "gpt-5.4")]
        self.turn["tokens"] = {key: value * 2 for key, value in self.turn["tokens"].items()}
        result = self.estimate()
        self.assertEqual(result["credits"], "5.037500")
        self.assertEqual(sum(Decimal(row["credits"]) for row in result["models"]), Decimal(result["credits"]))
        self.assertIsNone(result["rateDate"])
        self.assertTrue(result["historical"])
        self.assertIn("2026-09-27 历史", result["note"])
        self.assertEqual({row["standardRates"]["rateDate"] for row in result["models"]},
                         {"2026-09-27", "2026-09-30"})

    def test_prior_snapshot_retains_source_and_superseded_rates_for_audit_only(self):
        snapshot = HISTORICAL_RATE_SNAPSHOTS[HISTORICAL_RATE_DATE]
        self.assertEqual(snapshot["creditSourceUrl"], SOURCE_URL)
        self.assertEqual(snapshot["speedSourceUrl"], SPEED_SOURCE_URL)
        self.assertEqual(snapshot["creditFastMultipliers"]["gpt-6-astra"], "2.5")
        self.assertEqual(snapshot["creditRates"]["gpt-5.4-mini"][3], "113")
        self.assertEqual(snapshot["apiRates"]["gpt-5.4-mini"][3], "4.5")
        self.assertNotIn("gpt-6.1-sol", snapshot["creditRates"])
        self.assertNotIn("gpt-6.1-sol", snapshot["apiRates"])
        self.assertIsNot(snapshot["creditRates"], RATES)
        self.assertIsNot(snapshot["apiRates"], API_RATES)
        # Current replacement estimates do not purport to reconstruct old bills.
        self.turn["startedAt"] = "2026-09-27T01:00:00Z"
        self.settings["speedMode"] = "fast"
        self.assertEqual(self.estimate()["credits"], "22.000000")

    def test_gpt54_fast_multiplier_is_two(self):
        self.turn["model"] = "gpt-5.4"
        baseline = Decimal(self.estimate()["credits"])
        self.settings["speedMode"] = "fast"
        self.assertEqual(Decimal(self.estimate()["credits"]), baseline * 2)

    def test_mini_does_not_inherit_gpt54_fast_support(self):
        self.turn["model"] = "gpt-5.4-mini"
        self.assertEqual(self.estimate()["status"], "estimated")
        for speed in ("auto", "fast"):
            self.settings["speedMode"] = speed
            self.assertUnavailable(self.estimate())

    def test_all_known_model_rates(self):
        expected = {
            "gpt-6.1-sol": "302.500000", "gpt-6-sol": "305.000000", "gpt-6-luna": "15.250000",
            "gpt-6-astra": "1525.000000", "gpt-5.6-sol": "610.000000",
            "gpt-5.6-terra": "355.000000", "gpt-5.6-luna": "35.500000",
            "gpt-5.5": "887.500000", "gpt-5.4": "443.750000", "gpt-5.4-mini": "133.625000",
        }
        self.assertEqual(set(expected), set(RATES))
        self.turn["tokens"].update(input=2000000, cachedInput=1000000, output=1000000, total=3000000)
        for model, credits in expected.items():
            with self.subTest(model=model):
                self.turn["model"] = model
                self.assertEqual(self.estimate()["credits"], credits)

    def test_reasoning_is_output_subset_not_additional_charge(self):
        before = self.estimate()["credits"]
        self.turn["tokens"]["reasoningOutput"] = 0
        self.assertEqual(self.estimate()["credits"], before)

    def test_reasoning_effort_does_not_change_rate(self):
        before = self.estimate()
        for effort in ("ultra", "xhigh", "extra high"):
            with self.subTest(effort=effort):
                self.turn["reasoningEffort"] = effort
                self.assertEqual(self.estimate(), before)

    def test_confirmed_standard_never_uses_ambiguous_speed_midpoint(self):
        for tier in (None, "priority", "default"):
            with self.subTest(tier=tier):
                self.turn["serviceTier"] = tier
                result = self.estimate()
                self.assertEqual(result["estimateBasis"], "standard")
                self.assertEqual(result["credits"], "11.000000")
                self.assertNotIn("中点", result["note"])

    def test_default_speed_follows_confirmed_non_fast_preference(self):
        self.settings.pop("speedMode")
        result = self.estimate()
        self.assertEqual(result["estimateBasis"], "standard")
        self.assertEqual(result["credits"], "11.000000")

    def test_unknown_models_and_aliases_fail_closed(self):
        for model in (None, "gpt-5.3-codex-spark", "gpt-6-astra-latest", "gpt-5.6", "unknown"):
            with self.subTest(model=model):
                self.turn["model"] = model
                self.assertUnavailable(self.estimate())

    def test_spark_has_specific_unavailable_price_note(self):
        self.turn.update(model="gpt-5.3-codex-spark", reasoningEffort="xhigh")
        result = self.estimate()
        self.assertUnavailable(result)
        self.assertEqual(result["note"], "Spark 暂无公开数字费率，不能据此估算金额。")

    def test_mixed_metadata_cannot_be_overridden_with_one_rate(self):
        self.turn["pricingMetadataStatus"] = "mixed"
        for speed in ("auto", "standard", "fast"):
            self.settings["speedMode"] = speed
            self.assertUnavailable(self.estimate())

    def test_missing_metadata_fails_closed(self):
        self.turn.pop("pricingMetadataStatus")
        self.assertUnavailable(self.estimate())

    def test_partial_data_preserves_midpoint_with_incomplete_note(self):
        self.turn["quality"] = "partial"
        self.settings["speedMode"] = "auto"
        result = self.estimate()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["credits"], "16.500000")
        self.assertEqual(result["estimateBasis"], "midpoint")
        self.assertIsNone(result["creditsMax"])
        self.assertIn("不完整", result["note"])

    def test_running_estimate_is_labeled_so_far(self):
        self.turn["status"] = "running"
        self.assertIn("截至目前", self.estimate()["note"])

    def test_cache_write_tokens_are_not_guessed(self):
        self.turn["tokens"]["cacheWriteInput"] = 1
        result = self.estimate()
        self.assertUnavailable(result)
        self.assertIn("无独立缓存写入费", result["note"])
        self.assertIn("映射尚未核实", result["note"])

    def test_invalid_counter_data_never_produces_money(self):
        for field, value in (("input", True), ("input", -1), ("cachedInput", 100001), ("total", 1), ("reasoningOutput", 5001), ("output", "5000"), ("total", 10**31)):
            with self.subTest(field=field, value=value):
                turn = deepcopy(self.turn)
                turn["tokens"][field] = value
                self.assertUnavailable(self.estimate(turn=turn))

    def test_invalid_exchange_values_fail_closed(self):
        for field in ("usdPerCredit", "currencyPerUsd"):
            for value in ("NaN", "Infinity", "-1", "1e999999", "1e-999999", True, []):
                with self.subTest(field=field, value=value):
                    settings = {**self.settings, field: value}
                    self.assertUnavailable(self.estimate(settings=settings))

    def test_currency_conversion_uses_unrounded_values(self):
        self.settings["currencyPerUsd"] = "7.123456789"
        result = self.estimate()
        self.assertEqual(result["amount"], "3.134321")

    def test_decimal_rounding_does_not_depend_on_global_context(self):
        self.turn["model"] = "gpt-5.6-luna"
        self.turn["tokens"].update(input=1, cachedInput=1, output=0, total=1, reasoningOutput=0)
        with localcontext() as context:
            context.prec = 3
            result = self.estimate()
        self.assertEqual(result["credits"], "0.000001")
        self.assertEqual(result["usd"], "0.000000")

    def test_midpoint_uses_unrounded_credits_before_currency_conversion(self):
        self.turn["model"] = "gpt-5.6-luna"
        self.turn["tokens"].update(input=5, cachedInput=5, output=0, total=5, reasoningOutput=0)
        self.settings.update(speedMode="auto", usdPerCredit="1", currencyPerUsd="1000")
        result = self.estimate()
        # True midpoint is 0.00000375. Converting the rounded 0.000004 credits
        # to the selected currency would incorrectly display 0.004000.
        self.assertEqual(result["credits"], "0.000004")
        self.assertEqual(result["usd"], "0.000004")
        self.assertEqual(result["amount"], "0.003750")
        self.assertEqual(result["estimateBasis"], "midpoint")

    def test_no_successful_mode_emits_public_ranges(self):
        for mode in ("official", "custom"):
            for speed in ("auto", "standard", "fast", "ultrafast"):
                for quality in ("complete", "partial"):
                    with self.subTest(mode=mode, speed=speed, quality=quality):
                        self.settings.update(pricingMode=mode, speedMode=speed, ratePerMillion="2.5")
                        self.turn["quality"] = quality
                        result = self.estimate()
                        self.assertIn(result["status"], ("estimated", "partial"))
                        for key in ("creditsMax", "usdMax", "amountMax"):
                            self.assertIsNone(result[key])

    def test_zero_usage_is_distinct_from_unavailable(self):
        self.turn["tokens"] = {key: 0 for key in self.turn["tokens"]}
        self.assertEqual(self.estimate()["credits"], "0.000000")
        self.turn["tokens"] = None
        self.assertUnavailable(self.estimate())

    def test_custom_amount_has_no_official_credit_or_usd_claim(self):
        result = estimate_turn({"tokens": {"total": 1234567}}, {"pricingMode": "custom", "ratePerMillion": "2.5"})
        self.assertEqual(result["amount"], "3.086418")
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimateBasis"], "custom")
        for key in ("usd", "credits", "sourceUrl", "rateDate"):
            self.assertIsNone(result[key])
        self.assertIn("非官方", result["note"])

    def test_custom_partial_and_unconfigured_rate(self):
        self.settings.update(pricingMode="custom", ratePerMillion="2.5")
        self.turn["quality"] = "partial"
        self.assertEqual(self.estimate()["status"], "partial")
        self.settings["ratePerMillion"] = None
        self.assertUnavailable(self.estimate())

    def test_inputs_are_not_mutated(self):
        before_turn, before_settings = deepcopy(self.turn), deepcopy(self.settings)
        self.estimate()
        self.assertEqual(self.turn, before_turn)
        self.assertEqual(self.settings, before_settings)


if __name__ == "__main__":
    unittest.main()
