"""Preserve known-price calls beside unsupported cache-write observations."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from periods import summarize_periods
from pricing import estimate_turn
from usage_log import _Reader


STAMP = "2026-09-27T12:00:00+00:00"
NEXT_STAMP = "2026-09-28T12:00:00+00:00"


def tokens(write=0):
    return dict(input_tokens=100, cached_input_tokens=50, cache_write_input_tokens=write,
                output_tokens=20, reasoning_output_tokens=10, total_tokens=120)


def parse(writes, timestamps=None):
    reader = _Reader("cache-write-synthetic-thread")
    reader.handle({"type": "session_meta", "payload": {"id": "cache-write-synthetic-thread"}})
    reader.handle({"type": "event_msg", "timestamp": STAMP,
                   "payload": {"type": "task_started", "turn_id": "synthetic-turn"}})
    reader.handle({"type": "turn_context", "payload": {
        "turn_id": "synthetic-turn", "model": "gpt-6-astra", "service_tier": "standard"}})
    cumulative = dict.fromkeys(tokens(), 0)
    for index, write in enumerate(writes):
        last = tokens(write)
        for key in cumulative:
            cumulative[key] += last[key]
        reader.handle({"type": "event_msg", "timestamp": timestamps[index] if timestamps else STAMP,
                       "payload": {"type": "token_count", "info": {
                           "total_token_usage": dict(cumulative), "last_token_usage": last}}})
    reader.handle({"type": "event_msg", "timestamp": timestamps[-1] if timestamps else STAMP,
                   "payload": {"type": "task_complete", "turn_id": "synthetic-turn"}})
    return reader.result()["turns"][0]


class CacheWriteIsolationTests(unittest.TestCase):
    def settings(self, mode):
        return {"pricingMode": mode, "currencyPerUsd": "1", "usdPerCredit": "0.04",
                "speedMode": "standard", "subscriptionRenewalDay": 9}

    def test_parser_retains_supported_boundary_and_all_six_counters(self):
        record = parse([0, 20, 0])
        self.assertEqual(len(record["usageSlices"]), 2)
        self.assertEqual(sorted((part["tokens"]["total"], part["tokens"]["cacheWriteInput"])
                                for part in record["usageSlices"]), [(120, 20), (240, 0)])
        for field, value in record["tokens"].items():
            self.assertEqual(sum(part["tokens"][field] for part in record["usageSlices"]), value)

    def test_unpriced_call_does_not_erase_earlier_or_later_priced_calls(self):
        for mode in ("api", "official"):
            for writes in ([0, 20], [20, 0], [0, 20, 0]):
                with self.subTest(mode=mode, writes=writes):
                    record = parse(writes)
                    result = estimate_turn(record, self.settings(mode))
                    expected = Decimal("0.001550") * writes.count(0)
                    self.assertEqual(Decimal(result["amount"]), expected)
                    self.assertEqual(result["unpricedTokens"], 120)
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["models"][0]["tokens"]["total"], record["tokens"]["total"])
                    self.assertEqual(result["models"][0]["unpricedTokens"], 120)
                    self.assertTrue(result["models"][0]["partial"])
                    self.assertEqual(result["models"][0]["amount"], result["amount"])

    def test_periods_keep_priced_subtotal_and_count_turn_only_once(self):
        record = parse([0, 20, 0])
        day = datetime.fromisoformat(STAMP).astimezone().date()
        for mode in ("api", "official"):
            periods = summarize_periods([record, record], self.settings(mode), now=day)
            for key in ("today", "subscription"):
                with self.subTest(mode=mode, period=key):
                    period = periods[key]
                    self.assertEqual(period["amount"], "0.003100")
                    self.assertEqual(period["unpricedTokens"], 120)
                    self.assertEqual(period["unpricedTurnCount"], 1)
                    self.assertEqual(period["turnCount"], 1)
                    self.assertEqual(period["tokens"]["total"], 360)
                    self.assertEqual(period["models"][0]["tokens"], period["tokens"])

    def test_unsupported_future_day_does_not_remove_prior_day_amount(self):
        record = parse([0, 20], [STAMP, NEXT_STAMP])
        settings = self.settings("api")
        day = datetime.fromisoformat(STAMP).astimezone().date()
        today = summarize_periods([record], settings, now=day)["today"]
        self.assertEqual(today["amount"], "0.001550")
        self.assertEqual(today["unpricedTokens"], 0)
        self.assertEqual(today["status"], "complete")
        self.assertEqual(estimate_turn(record, settings)["amount"], "0.001550")

    def test_all_unsupported_remains_unavailable_not_zero(self):
        result = estimate_turn(parse([20, 20]), self.settings("api"))
        self.assertIsNone(result["amount"])
        self.assertEqual(result["unpricedTokens"], 240)
        self.assertEqual(result["status"], "unavailable")

    def test_legacy_merged_counter_cannot_invent_supported_portion(self):
        record = parse([0, 20])
        record.pop("usageSlices")
        result = estimate_turn(record, self.settings("api"))
        self.assertIsNone(result["amount"])
        self.assertEqual(result["unpricedTokens"], 240)

    def test_separate_slices_and_input_snapshots_are_preserved(self):
        record = parse([0, 20])
        before = deepcopy(record)
        result = estimate_turn(record, self.settings("api"))
        self.assertEqual(result["amount"], "0.001550")
        self.assertEqual(record, before)


if __name__ == "__main__":
    unittest.main()
