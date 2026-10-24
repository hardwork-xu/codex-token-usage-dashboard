"""Synthetic frozen residual allocation; no account, file or log access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from device_allocation import (build_allocation, validate_allocation, pricing_fingerprint,
                               DeviceAllocationError, MAX_ALLOCATION_BYTES)
from periods import summarize_periods
from pricing import estimate_turn


FIELDS = ("total", "input", "cachedInput", "cacheWriteInput", "output", "reasoningOutput")
NOW = datetime(2026, 10, 25, 1, 23, 45, tzinfo=timezone(timedelta(hours=8)))
SETTINGS = {"subscriptionRenewalDay": 9, "pricingMode": "official", "currencyPerUsd": "7.8",
            "ratePerMillion": "99999", "speedMode": "fast"}


def counters(incoming=1000, outgoing=100, cached=200, reasoning=20, writes=0):
    return dict(total=incoming + outgoing, input=incoming, output=outgoing,
                cachedInput=cached, reasoningOutput=reasoning, cacheWriteInput=writes)


def turn(thread="local", ident="turn1", *, day="2026-10-25", model="gpt-6-astra", tokens=None):
    tokens = counters() if tokens is None else tokens
    return {"id": ident, "threadId": thread, "quality": "complete", "model": model,
            "serviceTier": "priority", "pricingMetadataStatus": "known", "apiContext": "short",
            "tokens": tokens, "dailyUsage": {day: tokens} if day else {},
            "undatedTokens": dict.fromkeys(FIELDS, 0) if day else tokens,
            "sourceAvailability": "live", "status": "completed"}


def conversation(ident="local", *, missing=False, saved=True, source="live", gap=False):
    return {"id": ident, "reading": None if missing else {"complete": True},
            "readError": "synthetic missing" if missing else None,
            "readErrorCode": "source_missing" if missing else None,
            "archive": {"source": "archive" if missing and saved else "none" if missing else source,
                        "reason": "source_unavailable" if missing else None,
                        "historicalGap": missing, "hasSavedUsage": saved,
                        "continuation": {"status": "continuation_with_gap"} if gap else None}}


class DeviceAllocationTests(unittest.TestCase):
    def build(self, turns=None, conversations=None, **options):
        return build_allocation([turn()] if turns is None else turns,
                                [conversation()] if conversations is None else conversations,
                                options.get("settings", SETTINGS), options.get("now", NOW))

    def assert_conserved(self, value):
        for period in value["periods"].values():
            for key in FIELDS:
                n = lambda group: (period[group]["tokens"] or {}).get(key, 0)
                self.assertEqual(n("total"), n("local") + n("other"))
            usd = lambda group: Decimal(period[group]["usd"] or "0")
            self.assertEqual(usd("total"), usd("local") + usd("other"))

    def test_partitions_complete_ledger_using_only_local_log_evidence(self):
        a, b, c = turn(), turn("old"), turn("compressed")
        b["sourceAvailability"] = "archive"
        value = self.build([a, b, c], [conversation(), conversation("old", missing=True),
                                      conversation("compressed", source="compressed")])
        for period in value["periods"].values():
            self.assertEqual(period["total"]["tokens"]["total"], 3300)
            self.assertEqual(period["local"]["tokens"]["total"], 2200)
            self.assertEqual(period["other"]["tokens"]["total"], 1100)
        self.assert_conserved(value)

    def test_archive_turn_and_continuity_gap_remain_manual_residual(self):
        a = turn(); a["sourceAvailability"] = "archive"
        b = turn("gap")
        value = self.build([a, b], [conversation(), conversation("gap", gap=True)])
        self.assertEqual(value["periods"]["allTime"]["local"]["tokens"]["total"], 0)
        self.assertEqual(value["periods"]["allTime"]["other"]["tokens"]["total"], 2200)

    def test_verified_contiguous_history_includes_previously_saved_local_observations(self):
        row = conversation()
        row["archive"]["continuation"] = {"status": "overlap_verified", "retainedTurns": 4}
        result = self.build([turn()], [row])
        self.assertEqual(result["periods"]["allTime"]["local"]["tokens"]["total"], 1100)
        self.assertEqual(result["periods"]["allTime"]["other"]["tokens"]["total"], 0)

    def test_api_usd_forces_standard_and_matches_existing_period_pipeline(self):
        values = [turn(), turn("old", model="gpt-6-luna")]
        values[1]["sourceAvailability"] = "archive"
        report = self.build(values, [conversation(), conversation("old", missing=True)])
        expected = summarize_periods(values, {"pricingMode": "api", "currencyPerUsd": "1", "subscriptionRenewalDay": 9}, now=NOW)
        self.assertEqual(report["periods"]["today"]["total"]["usd"], expected["today"]["usd"])
        self.assertEqual(report["periods"]["subscription"]["total"]["usd"], expected["subscription"]["usd"])
        self.assert_conserved(report)

    def test_mixed_model_cache_context_slices_use_same_known_usd(self):
        a, b = counters(), counters(2000, 300, 1500, 100)
        total = {key: a[key] + b[key] for key in FIELDS}
        item = turn(tokens=total)
        item.update(model=None, pricingMetadataStatus="mixed", usageSlices=[
            {"day": "2026-10-25", "model": "gpt-6-astra", "serviceTier": "default", "pricingMetadataStatus": "known", "apiContext": "short", "tokens": a},
            {"day": "2026-10-25", "model": "gpt-6-luna", "serviceTier": "priority", "pricingMetadataStatus": "known", "apiContext": "long", "tokens": b}])
        result = self.build([item])
        expected = estimate_turn(item, {"pricingMode": "api", "currencyPerUsd": "1"})["usd"]
        for period in result["periods"].values():
            self.assertEqual(period["local"]["usd"], expected)
            self.assertEqual(period["local"]["tokens"], total)

    def test_unknown_model_and_unsupported_counter_money_stay_null(self):
        for item in [turn(model="gpt-5.3-codex-spark"), turn(tokens=counters(writes=10))]:
            result = self.build([item])
            for period in result["periods"].values():
                self.assertIsNone(period["total"]["usd"])
                self.assertIsNone(period["local"]["usd"])
                self.assertEqual(period["local"]["unpricedTokens"], 1100)
            self.assert_conserved(result)

    def test_known_money_subtotal_survives_unknown_residual(self):
        values = [turn(), turn("old", model="unknown-model")]
        result = self.build(values, [conversation(), conversation("old", missing=True)])
        row = result["periods"]["allTime"]
        self.assertEqual(row["total"]["usd"], row["local"]["usd"])
        self.assertIsNone(row["other"]["usd"])
        self.assertTrue(row["total"]["partial"])

    def test_unknown_tokens_are_not_filled_and_tasks_count_once(self):
        unknown = turn("unknown"); unknown["tokens"] = None
        result = self.build([unknown, {**unknown, "id": "second"}],
                            [conversation("unknown", missing=True), conversation("empty", missing=True, saved=False)])
        self.assertEqual(result["unknownTaskCount"], 2)
        for period in result["periods"].values():
            self.assertIsNone(period["total"]["tokens"])
            self.assertIsNone(period["total"]["usd"])
            self.assertEqual(period["total"]["unknownTurnCount"], 2)
            self.assertTrue(period["other"]["partial"])

    def test_missing_unknown_task_does_not_erase_existing_known_subtotals(self):
        result = self.build([turn()], [conversation(), conversation("missing", missing=True, saved=False)])
        self.assertEqual(result["unknownTaskCount"], 1)
        row = result["periods"]["today"]
        self.assertEqual(row["total"]["tokens"]["total"], 1100)
        self.assertIsNone(row["other"]["tokens"])
        self.assertIsNone(row["other"]["usd"])
        self.assertTrue(row["total"]["partial"])

    def test_partial_unknown_turn_in_known_task_is_not_extra_unknown_task(self):
        item = turn(ident="unknown"); item["tokens"] = None
        result = self.build([turn(), item])
        self.assertEqual(result["unknownTaskCount"], 0)
        self.assertEqual(result["periods"]["allTime"]["total"]["unknownTurnCount"], 1)

    def test_undated_history_only_enters_all_time(self):
        result = self.build([turn(day=None)])
        for key in ("today", "subscription"):
            self.assertIsNone(result["periods"][key]["total"]["tokens"])
            self.assertIsNone(result["periods"][key]["total"]["usd"])
        self.assertEqual(result["periods"]["allTime"]["total"]["tokens"]["total"], 1100)
        self.assertIsNotNone(result["periods"]["allTime"]["total"]["usd"])

    def test_local_calendar_and_subscription_boundaries_are_event_dated(self):
        values = [turn(ident="old", day="2026-10-08"), turn(ident="current", day="2026-10-09"), turn(ident="today")]
        result = self.build(values)
        self.assertEqual(result["localDate"], "2026-10-25")
        self.assertEqual(result["periods"]["today"]["total"]["tokens"]["total"], 1100)
        self.assertEqual(result["periods"]["subscription"]["total"]["tokens"]["total"], 2200)
        self.assertEqual(result["periods"]["allTime"]["total"]["tokens"]["total"], 3300)
        self.assertEqual(result["periods"]["subscription"]["startDate"], "2026-10-09")

    def test_missing_renewal_has_no_invented_period(self):
        result = self.build(settings={})
        period = result["periods"]["subscription"]
        self.assertIsNone(period["startDate"])
        self.assertTrue(all(period[group]["tokens"] is None and period[group]["usd"] is None for group in ("total", "local", "other")))

    def test_full_input_exceeds_ui_200_record_cap_and_duplicates_partition_once(self):
        values = [turn(ident=str(index)) for index in range(251)]
        values.append({**values[0], "sourceAvailability": "archive"})
        report = self.build(values)
        self.assertEqual(report["periods"]["allTime"]["total"]["tokens"]["total"], 251 * 1100)
        self.assertEqual(report["periods"]["allTime"]["other"]["tokens"]["total"], 1100)
        self.assert_conserved(report)

    def test_current_failures_cannot_be_folded_into_manual_residual(self):
        changes = [{"reading": {"complete": False}}, {"readError": "bad"},
                   {"readErrorCode": "identity_mismatch"},
                   {"archive": {"reason": "source_changed"}},
                   {"archive": {"writeErrors": 1}}, {"reading": None}]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(DeviceAllocationError, "collection_not_ready"):
                self.build(conversations=[{**conversation(), **change}])
        for code in ("source_unreadable", "identity_mismatch", None):
            row = conversation(missing=True); row["readErrorCode"] = code
            with self.subTest(code=code), self.assertRaises(DeviceAllocationError):
                self.build(conversations=[row])

    def test_snapshot_is_isolated_private_and_immutable_after_new_usage(self):
        item = turn(); item.update(prompt="PRIVATE_PROMPT", path="/PRIVATE_PATH", accountId="PRIVATE_ACCOUNT")
        conv = conversation(); conv["title"] = "PRIVATE_TITLE"
        original = deepcopy(item)
        report = self.build([item], [conv]); encoded = json.dumps(report)
        self.assertEqual(item, original)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("threadId", encoded)
        self.assertLess(len(encoded), MAX_ALLOCATION_BYTES)
        item["tokens"]["total"] += 1
        self.assertEqual(json.dumps(report), encoded)
        copy = validate_allocation(report); copy["periods"]["allTime"]["total"]["usd"] = None
        self.assertEqual(json.dumps(report), encoded)

    def test_pricing_fingerprint_excludes_fx_account_and_fast_settings(self):
        first = pricing_fingerprint(SETTINGS)
        self.assertEqual(first, pricing_fingerprint({**SETTINGS, "currencyPerUsd": "999", "speedMode": "standard", "accountId": "ignored"}))
        self.assertNotEqual(first, pricing_fingerprint({**SETTINGS, "subscriptionRenewalDay": 10}))

    def test_validator_rejects_private_keys_and_bad_numeric_or_date_values(self):
        report = self.build()
        changed = [lambda r: r.update(accountId="private"),
                   lambda r: r["periods"]["today"]["local"].update(path="private"),
                   lambda r: r.update(unknownTaskCount=True),
                   lambda r: r.update(capturedAt="2026-10-25T00:00:00"),
                   lambda r: r.update(localDate="2026-10-24"),
                   lambda r: r["periods"]["today"].update(startDate="2026-10-24"),
                   lambda r: r["periods"]["allTime"]["total"].update(usd=0.1),
                   lambda r: r["periods"]["allTime"]["total"].update(usd="NaN"),
                   lambda r: r["periods"]["allTime"]["total"].update(usd="-1.000000"),
                   lambda r: r["periods"]["allTime"]["total"]["tokens"].update(input=True),
                   lambda r: r["periods"]["allTime"]["total"]["tokens"].update(cachedInput=10000)]
        for mutate in changed:
            data = deepcopy(report); mutate(data)
            with self.subTest(mutate=mutate), self.assertRaises(DeviceAllocationError):
                validate_allocation(data)

    def test_validator_rejects_token_money_and_unknown_count_nonconservation(self):
        for field in ("usd", "unpricedTokens", "unassignedTokens", "unknownTurnCount", "tokens"):
            data = self.build(); row = data["periods"]["allTime"]["other"]
            if field == "tokens":
                row[field] = counters(1, 0, 0, 0)
            elif field == "usd":
                row[field] = "1.000000"
            else:
                row[field] = 1
            with self.subTest(field=field), self.assertRaises(DeviceAllocationError):
                validate_allocation(data)

    def test_validator_never_converts_all_unpriced_money_to_zero(self):
        result = self.build([turn(model="unknown-model")])
        result["periods"]["allTime"]["total"]["usd"] = "0.000000"
        with self.assertRaises(DeviceAllocationError):
            validate_allocation(result)

    def test_validator_does_not_relabel_unknown_values_as_complete(self):
        result = self.build([turn(model="unknown-model")])
        for period in result["periods"].values():
            period["total"]["partial"] = False
            period["local"]["partial"] = False
        with self.assertRaises(DeviceAllocationError):
            validate_allocation(result)

    def test_validator_rejects_period_counts_exceeding_lifetime(self):
        result = self.build()
        for group in ("total", "local"):
            result["periods"]["today"][group]["tokens"] = counters(2000, 200, 400, 40)
        with self.assertRaises(DeviceAllocationError):
            validate_allocation(result)

    def test_bad_clock_unknown_task_or_counter_fails_closed(self):
        for options in ({"now": datetime(2026, 10, 25)}, {"conversations": []},
                        {"turns": [{**turn(), "tokens": {"total": 5}}]}):
            with self.subTest(options=options), self.assertRaises(DeviceAllocationError):
                self.build(**options)


if __name__ == "__main__":
    unittest.main()
