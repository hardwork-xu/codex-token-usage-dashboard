"""Synthetic backend checks; never query a real account or read real transcripts."""
from __future__ import annotations

import contextlib
import copy
from datetime import date, timedelta
from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter


class QuotaTests(unittest.TestCase):
    def test_invalid_rpc_result_is_reported_as_unavailable(self):
        for invalid in (None, [], "invalid"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RuntimeError):
                    meter.normalize_quota(invalid)

    def test_null_window_is_unavailable_not_zero_usage(self):
        result = meter.normalize_quota({"rateLimitsByLimitId": {"codex": {
            "primary": None,
            "secondary": {"usedPercent": 16, "windowDurationMins": 10080, "resetsAt": 1234},
        }}})
        self.assertEqual(len(result["buckets"][0]["windows"]), 1)
        window = result["buckets"][0]["windows"][0]
        self.assertEqual(window["remainingPercent"], 84)
        self.assertEqual(window["windowMinutes"], 10080)

    def test_invalid_percentages_are_not_shown(self):
        for invalid in (None, True, False, "12", -1, 101, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                result = meter.normalize_quota({"rateLimits": {"primary": {"usedPercent": invalid}}})
                self.assertEqual(result["buckets"][0]["windows"], [])

    def test_fractional_percentage_is_preserved_without_inventing_precision(self):
        result = meter.normalize_quota({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 12.5}}})
        self.assertEqual(result["buckets"][0]["windows"][0]["remainingPercent"], 87.5)

    def test_new_buckets_take_precedence_over_legacy(self):
        result = meter.normalize_quota({"rateLimitsByLimitId": {"model": {"limitName": "Model", "primary": {"usedPercent": 70}}}, "rateLimits": {"primary": {"usedPercent": 1}}})
        self.assertEqual([item["id"] for item in result["buckets"]], ["model"])
        self.assertEqual(result["buckets"][0]["windows"][0]["remainingPercent"], 30)


class SettingsTests(unittest.TestCase):
    def test_subscription_renewal_day_can_be_unset_or_valid_day(self):
        self.assertIsNone(meter.validate_settings({})["subscriptionRenewalDay"])
        for day in (1, 9, 31):
            self.assertEqual(meter.validate_settings({"subscriptionRenewalDay": day})["subscriptionRenewalDay"], day)
        for day in (0, 32, True, 9.0, "9", [], {}):
            with self.subTest(day=day), self.assertRaises(ValueError):
                meter.validate_settings({"subscriptionRenewalDay": day})

    def test_defaults_use_api_in_usd_without_assuming_standard_speed(self):
        result = meter.validate_settings({})
        self.assertEqual((result["pricingMode"], result["usdPerCredit"], result["currencyPerUsd"], result["speedMode"]),
                         ("api", "0.04", "1", "auto"))
        self.assertEqual(result["speedOverrides"], {})
        self.assertEqual(result["currencyCode"], "USD")
        self.assertEqual(result["currencySymbol"], "$")
        self.assertEqual(result["exchangeRates"], {"CNY": "6.70842351", "USD": "1", "HKD": "7.84339018"})

    def test_currency_switching_canonicalizes_name_symbol_and_rate(self):
        settings = meter.validate_settings({})
        expected = {"CNY": ("人民币", "¥", "6.70842351"), "USD": ("美元", "$", "1"), "HKD": ("港元", "HK$", "7.84339018")}
        for code in ("CNY", "HKD", "USD", "CNY"):
            with self.subTest(code=code):
                settings = meter.validate_settings({**settings, "currencyCode": code,
                                                    "currencyName": "旧名称", "currencySymbol": "OLD", "currencyPerUsd": "999"})
                self.assertEqual((settings["currencyName"], settings["currencySymbol"], settings["currencyPerUsd"]), expected[code])
                self.assertEqual(settings["currencyCode"], code)

    def test_custom_pricing_preserves_existing_units_instead_of_applying_currency_preset(self):
        result = meter.validate_settings({"pricingMode": "custom", "currencyCode": "CNY",
                                          "currencyName": "积分", "currencySymbol": "pts", "currencyPerUsd": "3.25",
                                          "ratePerMillion": "12.34"})
        self.assertEqual(result["currencyCode"], "CUSTOM")
        self.assertEqual((result["currencyName"], result["currencySymbol"], result["currencyPerUsd"], result["ratePerMillion"]),
                         ("积分", "pts", "3.25", "12.34"))

    def test_invalid_currency_codes_and_exchange_rate_shapes_are_rejected(self):
        for code in ("EUR", "usd", "", [], {}, True):
            with self.subTest(code=code), self.assertRaises(ValueError):
                meter.validate_settings({"currencyCode": code})
        for rates in (None, [], {}, {"CNY": "7", "USD": "1"}, {**meter.EXCHANGE_RATES, "EUR": "0.9"}):
            with self.subTest(rates=rates), self.assertRaises(ValueError):
                meter.validate_settings({"exchangeRates": rates})

    def test_invalid_exchange_rates_and_nonunit_usd_anchor_are_rejected(self):
        for code in ("CNY", "HKD"):
            for invalid in (None, True, "", "0", "-1", "NaN", "Infinity", "1e-999999", "1000000001", [], {}):
                with self.subTest(code=code, invalid=invalid), self.assertRaises(ValueError):
                    meter.validate_settings({"exchangeRates": {**meter.EXCHANGE_RATES, code: invalid}})
        for anchor in ("0.99", "1.01", "2", True):
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                meter.validate_settings({"exchangeRates": {**meter.EXCHANGE_RATES, "USD": anchor}})
        result = meter.validate_settings({"exchangeRates": {**meter.EXCHANGE_RATES, "USD": "1.000"}})
        self.assertEqual(result["exchangeRates"]["USD"], "1")

    def test_validated_nested_rates_do_not_mutate_inputs_or_shared_defaults(self):
        defaults_before = copy.deepcopy(meter.DEFAULT_SETTINGS)
        source = {"exchangeRates": dict(meter.EXCHANGE_RATES), "currencyCode": "CNY"}
        source_before = copy.deepcopy(source)
        first = meter.validate_settings(source)
        second = meter.validate_settings({})
        first["exchangeRates"]["CNY"] = "999"
        self.assertEqual(source, source_before)
        self.assertEqual(meter.DEFAULT_SETTINGS, defaults_before)
        self.assertEqual(second["exchangeRates"], defaults_before["exchangeRates"])

    def test_invalid_credit_conversion_or_mode_is_rejected(self):
        for key in ("usdPerCredit", "currencyPerUsd"):
            for invalid in (None, True, [], {}, "", "0", "-1", "NaN", "Infinity", "1e-999999", "1e999999"):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    meter.validate_settings({key: invalid})
        for value in ({"pricingMode": "invalid"}, {"speedMode": "priority"}, {"speedMode": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                meter.validate_settings(value)

    def test_speed_overrides_accept_only_real_iso_dates_and_confirmed_speeds(self):
        source = {"2024-02-29": "fast", "2026-10-04": "standard", "2026-10-05": "ultrafast"}
        result = meter.validate_settings({"speedOverrides": source})
        self.assertEqual(result["speedOverrides"], source)
        result["speedOverrides"]["2026-10-04"] = "fast"
        self.assertEqual(source["2026-10-04"], "standard")
        self.assertEqual(meter.DEFAULT_SETTINGS["speedOverrides"], {})
        for invalid in (None, [], True, "fast", {"2026-02-29": "fast"},
                        {"20261005": "fast"}, {"2026-1-05": "fast"}, {"2026-W41-1": "fast"},
                        {"2026-10-05T00:00:00": "fast"}, {True: "fast"},
                        {"2026-10-05": "auto"}, {"2026-10-05": "priority"},
                        {"2026-10-05": True}, {"2026-10-05": []}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                meter.validate_settings({"speedOverrides": invalid})

    def test_speed_overrides_are_bounded_to_one_leap_year(self):
        overrides = {(date(2024, 1, 1) + timedelta(days=i)).isoformat(): "fast" for i in range(366)}
        self.assertEqual(meter.validate_settings({"speedOverrides": overrides})["speedOverrides"], overrides)
        overrides["2025-01-01"] = "fast"
        with self.assertRaises(ValueError):
            meter.validate_settings({"speedOverrides": overrides})

    def test_unconfigured_rate_does_not_claim_free_usage(self):
        self.assertIsNone(meter.validate_settings({})["ratePerMillion"])
        self.assertIsNone(meter.validate_settings({"ratePerMillion": ""})["ratePerMillion"])

    def test_decimal_input_is_preserved(self):
        result = meter.validate_settings({"currencyName": " 港币 ", "currencySymbol": "HK$", "ratePerMillion": "0.123456789012345678901234567"})
        self.assertEqual(result["currencyName"], "港币")
        self.assertEqual(result["ratePerMillion"], "0.123456789012345678901234567")

    def test_invalid_rates_are_rejected(self):
        for invalid in (True, False, "NaN", "sNaN", "Infinity", "-Infinity", "-0.01", "1000000001", "1e999999", [], {}, "not a number"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    meter.validate_settings({"ratePerMillion": invalid})

    def test_currency_fields_are_bounded(self):
        for value in ({"currencyName": ""}, {"currencyName": "a" * 25}, {"currencySymbol": "x" * 9}, {"currencySymbol": "x\n"}, {"extra": 1}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    meter.validate_settings(value)


class OriginTests(unittest.TestCase):
    def handler(self, **headers):
        value = meter.Handler.__new__(meter.Handler)
        value.server = SimpleNamespace(server_port=9876, meter=SimpleNamespace(csrf="correct-token"))
        value.headers = Message()
        for key, item in headers.items():
            value.headers[key] = item
        return value

    def test_same_origin_read_and_protected_write(self):
        self.assertTrue(self.handler(Host="127.0.0.1:9876").allowed())
        self.assertFalse(self.handler(Host="127.0.0.1:9876").allowed(write=True))
        self.assertTrue(self.handler(**{"Host": "127.0.0.1:9876", "Origin": "http://127.0.0.1:9876", "X-Meter-Token": "correct-token"}).allowed(write=True))

    def test_foreign_hosts_and_origins_are_denied_even_with_token(self):
        for changes in ({"Host": "localhost:9876"}, {"Host": "example.com:9876"}, {"Origin": "https://example.com"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            headers = {"Host": "127.0.0.1:9876", "X-Meter-Token": "correct-token", **changes}
            with self.subTest(changes=changes):
                self.assertFalse(self.handler(**headers).allowed(write=True))

    def test_non_ascii_token_is_denied_without_exception(self):
        self.assertFalse(self.handler(**{"Host": "127.0.0.1:9876", "X-Meter-Token": "é"}).allowed(write=True))


class SettingsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.subject = meter.Meter(self.folder)

    def post(self, settings):
        body = json.dumps(settings).encode("utf-8")
        handler = meter.Handler.__new__(meter.Handler)
        handler.server = SimpleNamespace(meter=self.subject)
        handler.path = "/api/settings"
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.allowed = mock.Mock(return_value=True)
        handler.respond = mock.Mock()
        handler.do_POST()
        return handler.respond.call_args

    def test_legacy_client_save_preserves_dates_and_explicit_empty_map_clears_them(self):
        overrides = {"2026-10-04": "standard", "2026-10-05": "fast"}
        meter.write_json(self.folder / "settings.json", meter.validate_settings({"speedMode": "standard", "speedOverrides": overrides}))
        result = self.post({"currencyCode": "CNY"})
        self.assertTrue(result.args[0]["ok"])
        self.assertEqual(self.subject.settings()["speedOverrides"], overrides)
        self.assertEqual(self.subject.settings()["speedMode"], "standard")
        self.assertEqual(self.subject.settings()["currencyCode"], "CNY")
        self.assertTrue(self.post({"speedOverrides": {}}).args[0]["ok"])
        self.assertEqual(self.subject.settings()["speedOverrides"], {})

    def test_full_year_of_speed_dates_can_round_trip_through_http(self):
        overrides = {(date(2024, 1, 1) + timedelta(days=i)).isoformat(): "ultrafast" for i in range(366)}
        settings = meter.validate_settings({"speedOverrides": overrides})
        self.assertGreater(len(json.dumps(settings)), 4096)
        self.assertTrue(self.post(settings).args[0]["ok"])
        self.assertEqual(self.subject.settings(), settings)

    def test_invalid_speed_dates_do_not_replace_saved_settings(self):
        saved = meter.validate_settings({"speedOverrides": {"2026-10-05": "fast"}})
        meter.write_json(self.folder / "settings.json", saved)
        result = self.post({"speedOverrides": {"2026-02-29": "fast"}})
        self.assertEqual(result.args[1], 400)
        self.assertEqual(self.subject.settings(), saved)


class LocalEndpointTests(unittest.TestCase):
    def test_only_exact_loopback_endpoints_are_allowed(self):
        for path in ("/", "/health", "/api/state"):
            self.assertTrue(meter.local_url("http://127.0.0.1:9876" + path))
        for url in ("http://127.0.0.1:123@otherhost/", "http://127.0.0.1:9876.evil.test/", "http://localhost:9876/", "https://127.0.0.1:9876/", "http://127.0.0.1:0/", "http://127.0.0.1:65536/", "http://127.0.0.1:9876/?query=1", "http://127.0.0.1:9876/#fragment", "http://127.0.0.1:9876/other"):
            with self.subTest(url=url):
                self.assertFalse(meter.local_url(url))

    def test_foreign_endpoint_is_rejected_before_network_access(self):
        with mock.patch.object(meter.request, "build_opener") as build_opener:
            with self.assertRaises(RuntimeError):
                meter.local_get("http://127.0.0.1:123@otherhost/")
        build_opener.assert_not_called()

    def test_redirects_are_rejected(self):
        with self.assertRaises(RuntimeError):
            meter.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://otherhost/")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.log = self.folder / "synthetic.jsonl"
        self.log.write_text("synthetic log", encoding="utf-8")
        meter.write_json(self.folder / "registry.json", {"test-thread": {"path": str(self.log)}})
        self.subject = meter.Meter(self.folder)
        self.result = {"turns": [{"threadId": "test-thread", "turnId": "turn-1", "startedAt": 123, "tokens": {"total": 1234567}}]}

    def test_unknown_rate_amount_is_unavailable(self):
        with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
            result = self.subject.snapshot()
        self.assertIsNone(result["turns"][0]["amount"])

    def test_cost_uses_decimal_and_declared_precision(self):
        meter.write_json(self.folder / "settings.json", {"ratePerMillion": "2.5"})
        with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
            result = self.subject.snapshot()
        self.assertEqual(result["turns"][0]["amount"], "3.086418")
        self.assertEqual(result["settings"]["pricingMode"], "custom")

    def test_blank_legacy_settings_migrate_to_usd_without_assumed_fx(self):
        meter.write_json(self.folder / "settings.json", {"currencyName": "人民币", "currencySymbol": "¥", "ratePerMillion": None})
        self.assertEqual(self.subject.settings(), meter.DEFAULT_SETTINGS)

    def test_snapshot_estimates_by_categories_at_confirmed_standard_point(self):
        meter.write_json(self.folder / "settings.json", {"pricingMode": "official", "speedMode": "standard"})
        turn = self.result["turns"][0]
        turn.update({"model": "gpt-6-astra", "serviceTier": None, "pricingMetadataStatus": "unknown", "quality": "complete",
                     "tokens": {"total": 105000, "input": 100000, "cachedInput": 90000,
                                "cacheWriteInput": 0, "output": 5000, "reasoningOutput": 2000}})
        with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
            result = self.subject.snapshot()
        price = result["turns"][0]["pricing"]
        self.assertEqual(price["status"], "estimated")
        self.assertEqual(price["estimateBasis"], "standard")
        self.assertEqual(price["credits"], "11.000000")
        self.assertEqual(price["usd"], "0.440000")
        self.assertEqual(price["amount"], "0.440000")
        self.assertTrue(all(price[key] is None for key in ("creditsMax", "usdMax", "amountMax")))

    def test_explicit_current_auto_setting_uses_one_midpoint_and_no_max_fields(self):
        settings = meter.validate_settings({"pricingMode": "official", "speedMode": "auto", "currencyCode": "USD"})
        meter.write_json(self.folder / "settings.json", settings)
        self.result["turns"][0].update({
            "model": "gpt-6-astra", "serviceTier": None, "pricingMetadataStatus": "unknown", "quality": "complete",
            "tokens": {"total": 105000, "input": 100000, "cachedInput": 90000, "cacheWriteInput": 0, "output": 5000, "reasoningOutput": 2000},
        })
        with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
            price = self.subject.snapshot()["turns"][0]["pricing"]
        self.assertEqual(price["estimateBasis"], "midpoint")
        self.assertEqual((price["credits"], price["usd"], price["amount"]), ("16.500000", "0.660000", "0.660000"))
        self.assertTrue(all(price[key] is None for key in ("creditsMax", "usdMax", "amountMax")))

    def test_saved_currency_presets_and_edited_rates_survive_reload(self):
        rates = {"CNY": "7.10", "USD": "1", "HKD": "7.82"}
        for code in ("CNY", "USD", "HKD"):
            with self.subTest(code=code):
                saved = meter.validate_settings({"currencyCode": code, "exchangeRates": rates})
                meter.write_json(self.folder / "settings.json", saved)
                reloaded = meter.Meter(self.folder).settings()
                self.assertEqual(reloaded, saved)
                self.assertEqual(reloaded["currencyCode"], code)
                self.assertEqual(reloaded["currencyPerUsd"], rates[code])

    def test_old_auto_setting_without_currency_code_is_not_reinterpreted_as_standard(self):
        legacy = {"currencyName": "美元", "currencySymbol": "$", "ratePerMillion": None,
                  "pricingMode": "official", "usdPerCredit": "0.04", "currencyPerUsd": "1", "speedMode": "auto"}
        meter.write_json(self.folder / "settings.json", legacy)
        migrated = self.subject.settings()
        self.assertEqual((migrated["currencyCode"], migrated["speedMode"]), ("USD", "auto"))
        self.assertEqual(migrated["usdPerCredit"], "0.04")

    def test_explicit_saved_standard_setting_survives_new_auto_default(self):
        meter.write_json(self.folder / "settings.json", {"pricingMode": "official", "speedMode": "standard"})
        self.assertEqual(self.subject.settings()["speedMode"], "standard")

    def test_fx_reference_customization_tracks_any_rate_not_only_selected_currency(self):
        with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
            initial = self.subject.snapshot()["fxReference"]
        self.assertEqual(initial["date"], "2026-09-14")
        self.assertIn("欧洲央行", initial["label"])
        self.assertEqual(initial["sourceUrl"], "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html")
        self.assertFalse(initial["customized"])
        for code in ("CNY", "HKD"):
            with self.subTest(changed=code):
                rates = {**meter.EXCHANGE_RATES, code: "8"}
                meter.write_json(self.folder / "settings.json", meter.validate_settings({"currencyCode": "USD", "exchangeRates": rates}))
                with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
                    state = self.subject.snapshot()
                self.assertEqual(state["settings"]["currencyCode"], "USD")
                self.assertTrue(state["fxReference"]["customized"])
        equivalent_rates = {"CNY": "6.708423510", "USD": "1.000", "HKD": "7.843390180"}
        meter.write_json(self.folder / "settings.json", meter.validate_settings({"exchangeRates": equivalent_rates}))
        with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
            self.assertFalse(self.subject.snapshot()["fxReference"]["customized"])

    def test_invalid_saved_settings_return_independent_nested_defaults(self):
        meter.write_json(self.folder / "settings.json", {"pricingMode": "official", "currencyCode": "INVALID"})
        # Isolate a failing regression from module globals used by other tests.
        defaults = copy.deepcopy(meter.DEFAULT_SETTINGS)
        with mock.patch.object(meter, "DEFAULT_SETTINGS", defaults):
            expected = copy.deepcopy(defaults)
            fallback = self.subject.settings()
            fallback["exchangeRates"]["CNY"] = "999"
            self.assertEqual(defaults, expected)
            self.assertEqual(self.subject.settings(), expected)

    def test_invalid_log_does_not_display_old_successful_total(self):
        with mock.patch.object(meter.UsageLogReader, "read", return_value=self.result):
            self.assertEqual(len(self.subject.snapshot()["turns"]), 1)
        self.log.write_text("changed invalid synthetic data", encoding="utf-8")
        with mock.patch.object(meter.UsageLogReader, "read", side_effect=ValueError("invalid")):
            result = self.subject.snapshot()
        self.assertEqual(result["turns"], [])
        self.assertEqual(result["monitoring"]["status"], "partial")
        self.assertIsNone(result["monitoring"]["lastUpdate"])

    def test_refresh_failure_is_explicit_and_does_not_replace_timestamp(self):
        self.subject.quota = {"updatedAt": 123, "error": None, "buckets": []}
        with mock.patch.object(meter, "fetch_quota", side_effect=RuntimeError("synthetic failure")):
            self.subject.refresh()
        self.assertEqual(self.subject.quota["updatedAt"], 123)
        self.assertIn("failure", self.subject.quota["error"])

    def test_corrupt_registry_is_reported_without_old_totals(self):
        for invalid in ([], None, "invalid"):
            with self.subTest(invalid=invalid):
                meter.write_json(self.folder / "registry.json", invalid)
                result = self.subject.snapshot()
                self.assertEqual(result["turns"], [])
                self.assertEqual(result["monitoring"]["status"], "partial")


class McpTests(unittest.TestCase):
    def run_messages(self, messages):
        output = io.StringIO()
        data = "\n".join(json.dumps(item) if not isinstance(item, str) else item for item in messages) + "\n"
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(sys, "stdin", io.StringIO(data)), contextlib.redirect_stdout(output):
            meter.mcp(Path(folder))
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_handshake_tool_list_and_notification(self):
        result = self.run_messages([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual([item["id"] for item in result], [1, 2])
        self.assertEqual(result[0]["result"]["capabilities"], {"tools": {}})
        self.assertEqual({tool["name"] for tool in result[1]["result"]["tools"]}, {"open_usage_meter", "get_usage_summary"})

    def test_summary_does_not_send_csrf_secret_to_model(self):
        state = {"turns": [{"turnId": str(i)} for i in range(20)], "csrfToken": "secret"}
        with mock.patch.object(meter, "ensure_service", return_value="http://127.0.0.1:9876/"), mock.patch.object(meter, "local_get", return_value=state):
            result = self.run_messages([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_usage_summary"}}])
        content = json.loads(result[0]["result"]["content"][0]["text"])
        self.assertNotIn("csrfToken", content)
        self.assertEqual(len(content["turns"]), 10)

    def test_bad_json_does_not_reuse_previous_request_id(self):
        result = self.run_messages([{"jsonrpc": "2.0", "id": 42, "method": "ping"}, "{"])
        self.assertEqual(sum(item.get("id") == 42 for item in result), 1)


if __name__ == "__main__":
    unittest.main()
