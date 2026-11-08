"""Exact-period settings and real local reader/cache integration."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
from analytics import build_analytics
from periods import summarize_periods
from usage_log import _Turn
from test_headless_collection import THREAD, records, context, usage, event
import test_meter


INTERVAL = {"startAt": "2026-10-16T08:00:00+08:00",
            "endAt": "2026-10-16T09:00:00+08:00", "timeZone": "Asia/Shanghai"}


class ExactSettingsTests(unittest.TestCase):
    setUp = test_meter.SettingsEndpointTests.setUp
    post = test_meter.SettingsEndpointTests.post

    def test_legacy_currency_client_preserves_interval_and_explicit_null_clears(self):
        self.assertTrue(self.post({"subscriptionPeriod": INTERVAL}).args[0]["ok"])
        self.assertTrue(self.post({"currencyCode": "HKD"}).args[0]["ok"])
        self.assertEqual(self.subject.settings()["subscriptionPeriod"], INTERVAL)
        self.assertTrue(self.post({"subscriptionPeriod": None}).args[0]["ok"])
        self.assertIsNone(self.subject.settings()["subscriptionPeriod"])

    def test_invalid_interval_cannot_replace_saved_settings(self):
        self.post({"subscriptionPeriod": INTERVAL})
        before = self.subject.settings()
        result = self.post({"subscriptionPeriod": {**INTERVAL, "endAt": INTERVAL["startAt"]}})
        self.assertEqual(result.args[1], 400)
        self.assertEqual(self.subject.settings(), before)


class ExactSummaryIntegrationTests(unittest.TestCase):
    def test_reader_cache_and_setting_changes_keep_full_history_and_exact_period(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "synthetic.jsonl"
            values = records(finish=False)
            boundary = usage(300, 200)
            boundary["timestamp"] = "2026-10-16T01:00:00Z"
            values += [context("gpt-6-luna", "priority"), boundary,
                       event("task_complete", turn_id="turn")]
            path.write_text("\n".join(json.dumps(value) for value in values) + "\n")
            meter.write_json(root / "registry.json", {THREAD: {"path": str(path)}})
            settings = meter.validate_settings({"subscriptionPeriod": INTERVAL})
            meter.write_json(root / "settings.json", settings)
            subject = meter.Meter(root)
            for _ in range(3):
                snapshot = subject.snapshot()
                self.assertEqual(snapshot["periods"]["subscription"]["tokens"]["total"], 100)
                self.assertEqual(snapshot["analytics"]["periods"]["subscription"]["tokens"]["total"], 100)
                self.assertEqual(snapshot["analytics"]["periods"]["subscription"]["usd"],
                                 snapshot["periods"]["subscription"]["usd"])
                self.assertEqual(snapshot["turns"][0]["tokens"]["total"], 300)
                self.assertNotIn("usageSegments", snapshot["turns"][0])
                self.assertNotIn("_subscriptionSelection", json.dumps(snapshot))
            original = deepcopy(subject.archive.load(THREAD)["result"])
            settings["subscriptionPeriod"]["endAt"] = "2026-10-16T09:00:01+08:00"
            meter.write_json(root / "settings.json", settings)
            updated = subject.snapshot()
            self.assertEqual(updated["periods"]["subscription"]["tokens"]["total"], 300)
            self.assertEqual(subject.archive.load(THREAD)["result"], original)

    def test_analytics_selected_day_models_and_ranking_use_clipped_events(self):
        start = datetime.fromisoformat(INTERVAL["startAt"]).timestamp()
        turn = _Turn("synthetic-turn", "synthetic-thread", start)
        tokens = {"input": 1000, "cachedInput": 800, "cacheWriteInput": 0,
                  "output": 200, "reasoningOutput": 50, "total": 1200}
        for stamp, model, tier in [(start, "gpt-6-astra", "default"),
                                   (start + 3599, "gpt-6-luna", "priority"),
                                   (start + 3600, "gpt-6-astra", "priority")]:
            turn.add(tokens, stamp, metadata={"model": model, "serviceTier": tier,
                                             "pricingMetadataStatus": "known"}, api_context="short")
        rows = [turn.export()]
        settings = {"subscriptionPeriod": INTERVAL, "pricingMode": "api", "currencyPerUsd": "1"}
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        analytics = build_analytics(rows, settings, now=now)
        custom = analytics["periods"]["subscription"]
        period = summarize_periods(rows, settings, now=now)["subscription"]
        self.assertEqual(custom["tokens"]["total"], 2400)
        self.assertEqual(custom["usd"], period["usd"])
        self.assertEqual(sum(day["tokens"]["total"] for day in custom["daily"]), 2400)
        self.assertEqual(custom["topConversations"][0]["tokens"]["total"], 2400)
        self.assertEqual({row["model"] for row in custom["models"]}, {"gpt-6-astra", "gpt-6-luna"})
        self.assertEqual(analytics["periods"]["today"]["tokens"]["total"], 3600)
        self.assertEqual(custom["endAt"], INTERVAL["endAt"])

    def test_fixed_period_charts_do_not_expand_with_today_or_drop_selected_future_events(self):
        start = datetime.fromisoformat(INTERVAL["startAt"]).timestamp()
        turn = _Turn("synthetic-turn", "synthetic-thread", start)
        turn.add({"input": 80, "cachedInput": 0, "cacheWriteInput": 0,
                  "output": 20, "reasoningOutput": 0, "total": 100}, start,
                 metadata={"model": "gpt-6-luna", "serviceTier": "default", "pricingMetadataStatus": "known"},
                 api_context="short")
        for year in (2024, 2029):
            with self.subTest(year=year):
                value = build_analytics([turn.export()], {"subscriptionPeriod": INTERVAL},
                                        now=datetime(year, 10, 16, tzinfo=timezone.utc))["periods"]["subscription"]
                self.assertLessEqual(len(value["daily"]), 2)
                self.assertEqual(value["tokens"]["total"], 100)


if __name__ == "__main__":
    unittest.main()
