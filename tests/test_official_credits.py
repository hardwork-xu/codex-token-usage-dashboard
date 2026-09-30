"""Synthetic official-credit contracts; no account, subprocess, or network access."""
from __future__ import annotations

import copy
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import official_credits as credits
import meter


THREAD_ID = "synthetic-thread-a"
NOW = 1_800_000_000
USD = {"currencyCode": "USD", "currencyPerUsd": "1"}


def quota(balance="12340.123456789012", *, account="synthetic-account-a", **changes):
    return {"accountScope": credits.account_scope(account),
            "credits": {"balance": balance, "hasCredits": True, "unlimited": False},
            "updatedAt": NOW, "error": None, **changes}


def response(*, micros=1_234_567, usd_micros=49_382, **changes):
    return {"threadUsage": {"threadId": THREAD_ID,
                            "estimatedUsageCreditsMicros": micros,
                            "estimatedUsageUsdMicros": usd_micros,
                            "groups": [{"model": "synthetic-model", "reasoningEffort": "high",
                                        "speed": "standard", "estimatedUsageCreditsMicros": micros,
                                        "inputTokens": 20, "cachedInputTokens": 8,
                                        "outputTokens": 3, "totalTokens": 23}], **changes}}


class BalanceTests(unittest.TestCase):
    def test_main_bucket_is_authoritative_and_duplicate_model_balances_are_not_added(self):
        source = {"rateLimitsByLimitId": {
            "codex": {"credits": {"balance": "12.345678901234", "hasCredits": True}},
            "synthetic-model": {"credits": {"balance": "12.345678901234"}}},
            "rateLimits": {"credits": {"balance": "999"}}}
        result = credits.credits_from_response(source)
        self.assertEqual(result["balance"], "12.345678901234")
        self.assertTrue(result["hasCredits"])

    def test_model_only_new_map_does_not_fallback_to_unrelated_legacy_balance(self):
        result = credits.credits_from_response({
            "rateLimitsByLimitId": {"synthetic-model": {"credits": {"balance": "30"}}},
            "rateLimits": {"limitId": "codex", "credits": {"balance": "40"}}})
        self.assertIsNone(result["balance"])

    def test_legacy_main_bucket_is_supported_but_other_legacy_bucket_is_not(self):
        for limit in (None, "codex", "synthetic-model"):
            with self.subTest(limit=limit):
                result = credits.credits_from_response({"rateLimitsByLimitId": {}, "rateLimits": {
                    "limitId": limit, "credits": {"balance": "7.25"}}})
                self.assertEqual(result["balance"], None if limit == "synthetic-model" else "7.25")

    def test_missing_balance_never_becomes_zero_or_an_api_estimate(self):
        for value in (None, {}, {"balance": None}, {"hasCredits": True}):
            with self.subTest(value=value):
                result = credits.credit_view(quota(credits=value), {}, USD, now=NOW, validated=True)
                self.assertIsNone(result["balance"])
                self.assertIsNone(result["usdEquivalent"])
                self.assertEqual(result["status"], "unavailable")

    def test_balance_decimal_precision_survives_currency_conversion(self):
        balance = "9007199254740993.123456789012"
        result = credits.credit_view(quota(balance), {}, USD, now=NOW, validated=True)
        self.assertEqual(result["balance"], balance)
        self.assertEqual(result["usdEquivalent"], "360287970189639.72493827156048")
        self.assertEqual(result["amountEquivalent"], result["usdEquivalent"])

    def test_invalid_balances_and_boolean_flags_are_not_coerced(self):
        for value in (0, 2.5, True, "NaN", "Infinity", "1e3", "+1", " 1", "1" * 20,
                      "1.1234567890123", [], {}):
            with self.subTest(value=value):
                result = credits.normalize_credits({"balance": value, "unlimited": 1, "hasCredits": "true"})
                self.assertEqual(result, {"balance": None, "unlimited": None, "hasCredits": None})

    def test_negative_and_zero_server_balances_are_preserved(self):
        for balance in ("-2.50", "0.000000"):
            with self.subTest(balance=balance):
                result = credits.credit_view(quota(balance), {}, USD, now=NOW, validated=True)
                self.assertEqual(result["balance"], balance)
                self.assertEqual(result["status"], "fresh")
                self.assertEqual(Decimal(result["usdEquivalent"]), Decimal(balance) * Decimal("0.04"))

    def test_unlimited_is_authoritative_and_has_no_invented_currency_amount(self):
        for balance in (None, "-1", "12340"):
            with self.subTest(balance=balance):
                value = quota(credits={"balance": balance, "unlimited": True})
                result = credits.credit_view(value, {}, USD, now=NOW, validated=True)
                self.assertTrue(result["unlimited"])
                self.assertEqual(result["status"], "fresh")
                self.assertIsNone(result["usdEquivalent"])
                self.assertIsNone(result["amountEquivalent"])

    def test_custom_units_do_not_claim_currency_equivalence(self):
        result = credits.credit_view(quota("3"), {}, {"currencyCode": "CUSTOM", "currencyPerUsd": "7"},
                                     now=NOW, validated=True)
        self.assertEqual(result["usdEquivalent"], "0.12")
        self.assertIsNone(result["amountEquivalent"])

    def test_balance_freshness_requires_recent_validated_success(self):
        cases = (({}, True, NOW, "fresh"), ({}, True, NOW + 119, "fresh"),
                 ({}, True, NOW + 120, "stale"), ({}, False, NOW, "stale"),
                 ({"error": "查询失败"}, True, NOW, "stale"),
                 ({"updatedAt": NOW + 6}, True, NOW, "stale"),
                 ({"updatedAt": True}, True, NOW, "stale"))
        for updates, validated, now, expected in cases:
            with self.subTest(updates=updates, validated=validated, now=now):
                result = credits.credit_view(quota(**updates), {}, USD, now=now, validated=validated)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["balance"], "12340.123456789012")

    def test_balance_view_drops_private_and_unexpected_fields(self):
        value = quota(credits={"balance": "10", "unlimited": False,
                               "privatePrompt": "synthetic-private-marker"},
                      accountId="synthetic-private-marker", authorization="synthetic-private-marker")
        result = credits.credit_view(value, {}, USD, now=NOW, validated=True)
        self.assertNotIn("synthetic-private-marker", json.dumps(result))
        self.assertNotIn("accountScope", result)

    def test_raw_error_details_are_not_exposed_by_balance_view(self):
        result = credits.credit_view(quota(error="synthetic-private-error-marker"), {}, USD,
                                     now=NOW, validated=True)
        self.assertEqual(result["status"], "stale")
        self.assertNotIn("synthetic-private-error-marker", json.dumps(result))


class BalanceHistoryTests(unittest.TestCase):
    def test_scope_hashes_are_stable_and_account_isolation_is_preserved(self):
        first = quota("100")
        second = quota("800", account="synthetic-account-b")
        self.assertEqual(first["accountScope"], credits.account_scope("synthetic-account-a"))
        self.assertNotEqual(first["accountScope"], second["accountScope"])
        ledger = credits.record_balance({}, first)
        ledger = credits.record_balance(ledger, second)
        changed = quota("90", updatedAt=NOW + 10)
        ledger = credits.record_balance(ledger, changed)
        first_view = credits.credit_view(changed, ledger, USD, now=NOW + 10, validated=True)
        second_view = credits.credit_view(second, ledger, USD, now=NOW + 10, validated=True)
        self.assertEqual(first_view["trackingStartBalance"], "100")
        self.assertEqual(first_view["netChange"], "-10")
        self.assertEqual(second_view["trackingStartBalance"], "800")
        self.assertEqual(second_view["netChange"], "0")
        self.assertNotIn("synthetic-account", json.dumps(ledger))

    def test_missing_scope_does_not_mix_anonymous_accounts(self):
        value = quota("10", accountScope=None)
        self.assertEqual(credits.record_balance({}, value), {})
        self.assertIsNone(credits.credit_view(value, {}, USD, now=NOW)["netChange"])

    def test_invalid_or_error_observations_do_not_change_history(self):
        initial = credits.record_balance({}, quota("10"))
        for updates in ({"credits": None}, {"error": "查询失败"}, {"updatedAt": False},
                        {"updatedAt": NOW - 1}):
            with self.subTest(updates=updates):
                result = credits.record_balance(initial, quota("9", **updates))
                self.assertEqual(result, initial)

    def test_identical_polls_and_decimal_reformatting_do_not_fill_history(self):
        initial = credits.record_balance({}, quota("100.00"))
        result = credits.record_balance(initial, quota("100", updatedAt=NOW + 10))
        self.assertEqual(len(result[quota()["accountScope"]]["history"]), 1)
        self.assertEqual(initial, result)

    def test_history_is_bounded_but_original_tracking_baseline_is_preserved(self):
        with mock.patch.object(credits, "MAX_HISTORY", 3):
            ledger = {}
            for offset, balance in enumerate(("100", "90", "80", "70", "60")):
                ledger = credits.record_balance(ledger, quota(balance, updatedAt=NOW + offset))
            entry = ledger[quota()["accountScope"]]
            self.assertEqual(entry["baseline"], {"observedAt": NOW, "balance": "100"})
            self.assertEqual([row["balance"] for row in entry["history"]], ["80", "70", "60"])
            view = credits.credit_view(quota("60", updatedAt=NOW + 4), ledger, USD,
                                       now=NOW + 4, validated=True)
            self.assertEqual(view["netChange"], "-40")

    def test_balance_change_is_never_labelled_spend_and_can_increase(self):
        ledger = credits.record_balance({}, quota("100"))
        for balance, change in (("70", "-30"), ("150", "50")):
            with self.subTest(balance=balance):
                value = quota(balance, updatedAt=NOW + 10)
                view = credits.credit_view(value, ledger, USD, now=NOW + 10, validated=True)
                self.assertEqual(view["netChange"], change)
                self.assertNotIn("consumed", view)
                self.assertNotIn("spent", view)
                self.assertIn("不能视作实际消耗", view["note"])

    def test_history_and_prior_inputs_cannot_be_mutated_through_views(self):
        ledger = credits.record_balance({}, quota("100"))
        before = copy.deepcopy(ledger)
        view = credits.credit_view(quota("90", updatedAt=NOW + 10), ledger, USD, now=NOW + 10)
        view["history"][0]["balance"] = "0"
        updated = credits.record_balance(ledger, quota("80", updatedAt=NOW + 20))
        self.assertEqual(ledger, before)
        self.assertNotEqual(updated, ledger)


class ThreadUsageTests(unittest.TestCase):
    def test_micros_are_divided_by_one_million_without_float_rounding(self):
        source = response(micros=9_007_199_254_740_993, usd_micros=1)
        with mock.patch.object(credits.time, "time", return_value=NOW):
            result = credits.normalize_thread_usage(source, THREAD_ID)
        self.assertEqual(result["credits"], "9007199254.740993")
        self.assertEqual(result["usd"], "0.000001")
        self.assertEqual(result["groups"][0]["credits"], "9007199254.740993")
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["updatedAt"], NOW)

    def test_server_usd_is_preserved_independently_of_credit_price_assumptions(self):
        result = credits.normalize_thread_usage(response(micros=1_000_000, usd_micros=123_456), THREAD_ID)
        self.assertEqual(result["credits"], "1.000000")
        self.assertEqual(result["usd"], "0.123456")

    def test_null_thread_usage_never_uses_account_totals_or_local_estimates(self):
        for missing in (None, {}, {"threadId": "synthetic-other-thread"}):
            with self.subTest(missing=missing):
                result = credits.normalize_thread_usage({"threadUsage": missing,
                    "summary": {"lifetimeTokens": 9999}, "credits": "100", "localEstimate": "10"}, THREAD_ID)
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["credits"])
                self.assertIsNone(result["usd"])
                self.assertEqual(result["groups"], [])

    def test_invalid_credit_counters_do_not_turn_into_zero(self):
        for value in (None, True, False, -1, 1.5, "1000000", 10**30 + 1):
            with self.subTest(value=value):
                result = credits.normalize_thread_usage(response(micros=value), THREAD_ID)
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["credits"])

    def test_zero_server_usage_is_valid_and_missing_usd_stays_unknown(self):
        result = credits.normalize_thread_usage(response(micros=0, usd_micros=None, groups=[]), THREAD_ID)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["credits"], "0.000000")
        self.assertIsNone(result["usd"])

    def test_all_accepted_integer_sizes_survive_the_public_view(self):
        with mock.patch.object(credits.time, "time", return_value=NOW):
            normalized = credits.normalize_thread_usage(response(micros=10**30), THREAD_ID)
        view = credits.thread_usage_view(normalized, now=NOW, validated=True)
        self.assertEqual(view["status"], "fresh")
        self.assertEqual(view["credits"], "1000000000000000000000000.000000")

    def test_group_counters_are_allowlisted_and_invalid_numbers_stay_unknown(self):
        source = response()
        group = source["threadUsage"]["groups"][0]
        group.update(inputTokens=True, cachedInputTokens=-1, outputTokens=1.0,
                     totalTokens=9_007_199_254_740_993, prompt="synthetic-private-marker",
                     apiKey="synthetic-private-marker", model="synthetic\nmodel")
        source["threadUsage"]["prompt"] = "synthetic-private-marker"
        source["privateResponse"] = "synthetic-private-marker"
        result = credits.normalize_thread_usage(source, THREAD_ID)
        self.assertNotIn("synthetic-private-marker", json.dumps(result))
        self.assertIsNone(result["groups"][0]["model"])
        self.assertEqual(result["groups"][0]["tokens"], {
            "input": None, "cachedInput": None, "output": None, "total": 9_007_199_254_740_993})

    def test_malformed_or_excessive_group_payload_is_not_trusted(self):
        for groups in (None, {}, [None] * 501):
            with self.subTest(groups_type=type(groups).__name__):
                self.assertEqual(credits.normalize_thread_usage(response(groups=groups), THREAD_ID)["status"],
                                 "unavailable")
        result = credits.normalize_thread_usage(response(groups=[None, {}, {"estimatedUsageCreditsMicros": -1}]), THREAD_ID)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["credits"], "1.234567")

    def test_server_total_is_not_recomputed_by_adding_groups(self):
        value = response(micros=4_000_000)
        value["threadUsage"]["groups"] *= 2
        result = credits.normalize_thread_usage(value, THREAD_ID)
        self.assertEqual(result["credits"], "4.000000")

    def test_thread_view_requires_validated_recent_data(self):
        with mock.patch.object(credits.time, "time", return_value=NOW):
            normalized = credits.normalize_thread_usage(response(), THREAD_ID)
        for validated, now, expected in ((True, NOW, "fresh"), (False, NOW, "stale"),
                                          (True, NOW + 119, "fresh"), (True, NOW + 120, "stale")):
            with self.subTest(validated=validated, now=now):
                result = credits.thread_usage_view(normalized, now=now, validated=validated)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["credits"], "1.234567")

    def test_thread_view_drops_unexpected_cached_fields_and_raw_errors(self):
        with mock.patch.object(credits.time, "time", return_value=NOW):
            normalized = credits.normalize_thread_usage(response(), THREAD_ID)
        normalized.update(error="synthetic-private-marker", note="synthetic-private-marker")
        normalized["groups"][0]["unexpected"] = "synthetic-private-marker"
        result = credits.thread_usage_view(normalized, now=NOW, validated=True)
        self.assertEqual(result["status"], "stale")
        self.assertNotIn("synthetic-private-marker", json.dumps(result))

    def test_thread_view_does_not_share_mutable_groups_with_cached_record(self):
        normalized = credits.normalize_thread_usage(response(), THREAD_ID)
        before = copy.deepcopy(normalized)
        view = credits.thread_usage_view(normalized, now=NOW, validated=True)
        view["groups"][0]["tokens"]["input"] = 0
        self.assertEqual(normalized, before)


class RpcContractTests(unittest.TestCase):
    def rpc(self, responses):
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        rpc.responses.return_value = iter(responses)
        return rpc

    def test_fetch_uses_only_read_only_public_usage_rpc_and_closes_process(self):
        rpc = self.rpc([{"id": True, "result": {}}, {"id": 1, "result": {}},
                        {"id": 2, "result": response()}])
        with mock.patch("rpc_transport.JsonRpcProcess", return_value=rpc) as transport:
            result = credits.fetch_thread_usage(THREAD_ID, ["synthetic-codex"])
        transport.assert_called_once_with(["synthetic-codex", "app-server", "--stdio"], timeout=20)
        calls = [call.args[0] for call in rpc.send.call_args_list]
        self.assertEqual([call["method"] for call in calls], ["initialize", "initialized", "account/usage/read"])
        self.assertEqual(calls[-1]["params"], {"threadId": THREAD_ID})
        self.assertEqual(result["credits"], "1.234567")
        rpc.__exit__.assert_called_once()

    def test_rpc_server_errors_never_echo_private_data(self):
        cases = ([{"id": 1, "error": {"message": "synthetic-private-marker"}}],
                 [{"id": 1, "result": {}}, {"id": 2, "error": {"message": "synthetic-private-marker"}}])
        for responses in cases:
            with self.subTest(responses=responses):
                rpc = self.rpc(responses)
                with mock.patch("rpc_transport.JsonRpcProcess", return_value=rpc), self.assertRaises(RuntimeError) as caught:
                    credits.fetch_thread_usage(THREAD_ID, ["synthetic-codex"])
                self.assertNotIn("synthetic-private-marker", str(caught.exception))
                rpc.__exit__.assert_called_once()

    def test_os_and_timeout_errors_are_sanitized_without_launching_any_process(self):
        for exception in (OSError("synthetic-private-marker"), TimeoutError("synthetic-private-marker")):
            with self.subTest(kind=type(exception).__name__):
                with mock.patch("rpc_transport.JsonRpcProcess", side_effect=exception), self.assertRaises(RuntimeError) as caught:
                    credits.fetch_thread_usage(THREAD_ID, ["synthetic-codex"])
                self.assertNotIn("synthetic-private-marker", str(caught.exception))

    def test_closed_rpc_does_not_substitute_local_estimates(self):
        rpc = self.rpc([{"id": 1, "result": {}}])
        with mock.patch("rpc_transport.JsonRpcProcess", return_value=rpc), self.assertRaises(RuntimeError):
            credits.fetch_thread_usage(THREAD_ID, ["synthetic-codex"])
        rpc.__exit__.assert_called_once()


class MeterOfficialUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="official-credit-fixture-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.pending = []
        self.clock = [NOW]
        self.patch(meter.time, "time", side_effect=lambda: self.clock[0])
        self.patch(meter, "fetch_quota", side_effect=AssertionError("No real quota RPC"))
        self.patch(meter, "fetch_conversation_titles", side_effect=AssertionError("No real title RPC"))
        self.patch(meter.subprocess, "Popen", side_effect=AssertionError("No real subprocess"))
        self.patch(meter, "codex_command", return_value=["synthetic-codex"])
        self.fetch = self.patch(meter, "fetch_thread_usage",
                                return_value=credits.normalize_thread_usage(response(), THREAD_ID))
        self.patch(meter.threading, "Thread", side_effect=self.queue_thread)
        reader = mock.Mock()
        reader.read.return_value = {"turns": [], "warnings": [],
                                    "reading": {"complete": True, "bytesRead": 0, "fileBytes": 0}}
        self.patch(meter, "UsageLogReader", return_value=reader)
        self.register(THREAD_ID)
        self.subject = meter.Meter(self.folder)
        self.subject.quota = quota("100")
        self.subject.quota_verified = True

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def queue_thread(self, *, target, daemon):
        self.assertTrue(daemon)
        worker = mock.Mock()
        worker.start.side_effect = lambda: self.pending.append(target)
        return worker

    def finish(self):
        self.assertEqual(len(self.pending), 1)
        self.pending.pop()()

    def register(self, *ids):
        registry = {thread_id: {"path": str(self.folder / "synthetic.jsonl"), "registeredAt": NOW}
                    for thread_id in ids}
        (self.folder / "registry.json").write_text(json.dumps(registry), encoding="utf-8")

    def switch_account(self, account):
        self.subject.quota = quota("300", account=account, updatedAt=self.clock[0])

    def test_only_explicitly_registered_thread_ids_can_trigger_rpc(self):
        for invalid in (None, True, [], {}, "", "x" * 201, "synthetic-unregistered"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.subject.start_official_usage_refresh(invalid)
        self.assertEqual(self.pending, [])
        self.fetch.assert_not_called()
        self.assertFalse(self.subject.official_usage_lock.locked())

    def test_sixty_second_cooldown_and_busy_worker_prevent_duplicate_queries(self):
        self.assertEqual(self.subject.start_official_usage_refresh(THREAD_ID)["status"], "started")
        self.assertEqual(self.subject.start_official_usage_refresh(THREAD_ID)["status"], "running")
        self.finish()
        self.clock[0] += 59
        throttled = self.subject.start_official_usage_refresh(THREAD_ID)
        self.assertEqual(throttled, {"status": "throttled", "retryAfterSeconds": 1})
        self.assertFalse(self.subject.official_usage_lock.locked())
        self.clock[0] += 1
        self.assertEqual(self.subject.start_official_usage_refresh(THREAD_ID)["status"], "started")
        self.finish()
        self.assertEqual(self.fetch.call_count, 2)

    def test_null_official_record_remains_unavailable_in_snapshot(self):
        self.fetch.return_value = credits.normalize_thread_usage({"threadUsage": None}, THREAD_ID)
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        result = self.subject.snapshot()["officialUsage"][THREAD_ID]
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["credits"])
        self.assertIsNone(result["usd"])
        self.assertFalse(result["refreshing"])

    def test_failure_preserves_same_account_last_success_as_stale(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        self.clock[0] += 60
        self.fetch.side_effect = RuntimeError("synthetic-private-error-marker")
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        result = self.subject.snapshot()["officialUsage"][THREAD_ID]
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["credits"], "1.234567")
        self.assertEqual(result["updatedAt"], NOW)
        self.assertNotIn("synthetic-private-error-marker", json.dumps(result))
        self.assertFalse(self.subject.official_usage_lock.locked())

    def test_account_change_hides_existing_other_account_usage(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        self.switch_account("synthetic-account-b")
        self.assertNotIn(THREAD_ID, self.subject.snapshot()["officialUsage"])

    def test_interleaved_cache_replacement_cannot_relabel_a_captured_record(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        captured_account_a = copy.deepcopy(self.subject.official_usage[THREAD_ID])
        self.switch_account("synthetic-account-b")
        account_b_record = {
            **credits.normalize_thread_usage(response(micros=2_000_000), THREAD_ID),
            "accountScope": self.subject.quota["accountScope"],
        }

        class ReplacedDuringSnapshot(dict):
            def items(self):
                captured = list(super().items())
                self[THREAD_ID] = account_b_record
                return captured

        self.subject.official_usage = ReplacedDuringSnapshot({THREAD_ID: captured_account_a})
        self.assertNotIn(THREAD_ID, self.subject.snapshot()["officialUsage"])
        self.assertEqual(self.subject.official_usage[THREAD_ID], account_b_record)
        next_view = self.subject.snapshot()["officialUsage"][THREAD_ID]
        self.assertEqual(next_view["credits"], "2.000000")
        self.assertNotIn("accountScope", next_view)

    def test_snapshot_uses_one_captured_account_for_balance_and_usage(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        switch_account = self.switch_account

        class AccountChangesDuringSnapshot(dict):
            def items(self):
                captured = list(super().items())
                switch_account("synthetic-account-b")
                return captured

        self.subject.official_usage = AccountChangesDuringSnapshot(self.subject.official_usage)
        result = self.subject.snapshot()
        self.assertEqual(result["officialCredits"]["balance"], "100")
        self.assertEqual(result["officialUsage"][THREAD_ID]["credits"], "1.234567")
        self.assertEqual(self.subject.quota["credits"]["balance"], "300")
        next_view = self.subject.snapshot()
        self.assertEqual(next_view["officialCredits"]["balance"], "300")
        self.assertNotIn(THREAD_ID, next_view["officialUsage"])

    def test_unknown_or_unverified_account_cannot_start_or_consume_cooldown(self):
        for scope, validated in ((None, True), (quota()["accountScope"], False)):
            with self.subTest(scope_known=scope is not None, validated=validated):
                self.subject.quota = quota("100", accountScope=scope)
                self.subject.quota_verified = validated
                with self.assertRaisesRegex(ValueError, "先刷新官方余额"):
                    self.subject.start_official_usage_refresh(THREAD_ID)
                self.assertFalse(self.subject.official_usage_lock.locked())
                self.assertEqual(self.subject.official_usage_attempts, {})
                self.assertEqual(self.pending, [])
                self.fetch.assert_not_called()
        self.subject.quota = quota("100")
        self.subject.quota_verified = True
        self.assertEqual(self.subject.start_official_usage_refresh(THREAD_ID)["status"], "started")
        self.finish()

    def test_account_change_during_success_discards_the_old_account_response(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.switch_account("synthetic-account-b")
        self.finish()
        self.assertNotIn(THREAD_ID, self.subject.snapshot()["officialUsage"])
        self.assertNotIn(THREAD_ID, self.subject.official_usage)
        self.assertFalse(self.subject.official_usage_lock.locked())

    def test_failed_new_account_query_does_not_relabel_old_account_usage(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        self.clock[0] += 60
        self.switch_account("synthetic-account-b")
        self.fetch.side_effect = RuntimeError("synthetic-private-error-marker")
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        result = self.subject.snapshot()["officialUsage"].get(THREAD_ID)
        if result is not None:
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["credits"])
            self.assertEqual(result["groups"], [])

    def test_account_change_during_failure_cannot_overwrite_a_cached_result(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        before = copy.deepcopy(self.subject.official_usage)
        self.clock[0] += 60
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.switch_account("synthetic-account-b")
        self.fetch.side_effect = RuntimeError("synthetic-private-error-marker")
        self.finish()
        self.assertEqual(self.subject.official_usage, before)
        self.assertNotIn(THREAD_ID, self.subject.snapshot()["officialUsage"])

    def test_account_switch_does_not_inherit_other_account_query_cooldown(self):
        self.subject.start_official_usage_refresh(THREAD_ID)
        self.finish()
        self.switch_account("synthetic-account-b")
        self.assertEqual(self.subject.start_official_usage_refresh(THREAD_ID)["status"], "started")
        self.finish()

    def test_cold_start_does_not_treat_persisted_balance_as_fresh(self):
        meter.write_json(self.folder / "quota.json", quota("20"))
        restarted = meter.Meter(self.folder)
        result = restarted.snapshot()["officialCredits"]
        self.assertEqual(result["balance"], "20")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(restarted.snapshot()["officialUsage"], {})


if __name__ == "__main__":
    unittest.main()
