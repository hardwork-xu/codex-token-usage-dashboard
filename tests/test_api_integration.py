"""Synthetic log -> model slices -> API comparison -> calendar totals."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meter import validate_settings
from periods import summarize_periods
from pricing import estimate_turn
from usage_log import _Reader


class ApiIntegrationTests(unittest.TestCase):
    def test_request_bands_and_model_rates_survive_all_aggregation_layers(self):
        stamp = "2026-09-27T12:00:00+00:00"
        day = datetime.fromisoformat(stamp).astimezone().date()
        reader = _Reader("synthetic-api-thread")
        reader.handle({"type": "session_meta", "payload": {"id": "synthetic-api-thread"}})
        reader.handle({"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}})
        cumulative = dict(total_tokens=0, input_tokens=0, cached_input_tokens=0,
                          cache_write_input_tokens=0, output_tokens=0, reasoning_output_tokens=0)
        for model, incoming, cached, outgoing in [
            ("gpt-6-astra", 272000, 200000, 1000),
            ("gpt-6-astra", 272001, 200000, 1000),
            ("gpt-6-luna", 100000, 90000, 5000),
            (None, 100, 0, 0),
        ]:
            reader.handle({"type": "turn_context", "payload": {
                "turn_id": "turn-1", "model": model, "service_tier": "standard"}})
            last = dict(total_tokens=incoming + outgoing, input_tokens=incoming,
                        cached_input_tokens=cached, cache_write_input_tokens=0,
                        output_tokens=outgoing, reasoning_output_tokens=outgoing // 2)
            for key in cumulative:
                cumulative[key] += last[key]
            reader.handle({"timestamp": stamp, "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": dict(cumulative), "last_token_usage": last}}})
        reader.handle({"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}})
        turn = reader.result()["turns"][0]
        settings = validate_settings({"subscriptionRenewalDay": 9})
        self.assertEqual(settings["pricingMode"], "api")
        prices = [estimate_turn(turn, settings)]
        prices.extend(summarize_periods([turn, turn], settings, now=day)[key]
                      for key in ("today", "subscription"))
        for price in prices:
            self.assertEqual(price["usd"], "2.889420")
            self.assertEqual(price["amount"], "2.889420")
            self.assertIsNone(price["credits"])
            self.assertEqual(price["apiLongContextTokens"], 273001)
            self.assertEqual(price["apiContextUncertainTokens"], 0)
            self.assertEqual(price["unpricedTokens"], 100)
            self.assertEqual(price["status"], "partial")
            self.assertEqual(sum(Decimal(row["usd"]) for row in price["models"] if row["usd"] is not None), Decimal(price["usd"]))
            self.assertEqual(sum(row["tokens"]["total"] for row in price["models"]), 651101)

    def test_empty_api_period_is_zero_dollars_without_invented_credits(self):
        settings = validate_settings({"subscriptionRenewalDay": 9})
        for period in (summarize_periods([], settings)[key] for key in ("today", "subscription")):
            self.assertEqual(period["usd"], "0.000000")
            self.assertEqual(period["amount"], "0.000000")
            self.assertIsNone(period["credits"])


if __name__ == "__main__":
    unittest.main()
