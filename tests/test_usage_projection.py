"""Display projection preserves the parser's complete numeric observations."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from usage_log import UsageLogError, _Turn, reproject_usage as original_projection
from usage_projection import reproject_usage


def fixture():
    when = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    turn = _Turn("fixture-turn", "fixture-thread", when)
    for i, (model, tier, timestamp, context, write) in enumerate([
        ("gpt-6-astra", "standard", when, "short", 0),
        ("gpt-6.1-sol", "fast", when + 86400, "long", 0),
        (None, None, None, "unknown", 0),
        ("gpt-5.5", None, when, "short", 2),
    ]):
        counters = {"total": 100 + i, "input": 80, "cachedInput": 20,
                    "cacheWriteInput": write, "output": 20 + i, "reasoningOutput": 5}
        turn.add(counters, timestamp,
                 metadata={"model": model, "serviceTier": tier,
                           "pricingMetadataStatus": "known" if tier else "unknown"},
                 api_context=context,
                 provenance={"eventOrdinal": 10 + i, "eventPart": 0, "responseId": "fixture-response-" + str(i)})
    return {"threadId": "fixture-thread", "identityVerified": True,
            "turns": [turn.export()], "reading": {"complete": True}}, when


class ProjectionTests(unittest.TestCase):
    def test_full_projection_matches_existing_numeric_contract(self):
        source, when = fixture()
        windows = {"2026-10-05": {"startAt": int(when) - 43200, "endAt": int(when) + 43200,
                                   "timeZone": "UTC"}}
        self.assertEqual(reproject_usage(source, windows), original_projection(source, windows))
        self.assertEqual(reproject_usage(source), original_projection(source))

    def test_light_projection_matches_full_without_event_payload(self):
        source, _ = fixture()
        expected = original_projection(source)
        for turn in expected["turns"]:
            turn.pop("usageSegments", None)
        self.assertEqual(reproject_usage(source, include_segments=False), expected)

    def test_outputs_never_alias_source_or_other_projection(self):
        source, _ = fixture()
        before = deepcopy(source)
        full = reproject_usage(source)
        light = reproject_usage(source, include_segments=False)
        full["turns"][0]["usageSegments"][0]["tokens"]["input"] = 999
        full["turns"][0]["tokens"]["input"] = 998
        light["turns"][0]["usageSlices"][0]["tokens"]["input"] = 997
        light["reading"]["complete"] = False
        self.assertEqual(source, before)
        self.assertTrue(full["reading"]["complete"])

    def test_counter_contradiction_is_rejected_in_both_modes(self):
        source, _ = fixture()
        source["turns"][0]["tokens"]["total"] += 1
        for retain in (True, False):
            with self.subTest(retain=retain), self.assertRaises(UsageLogError):
                reproject_usage(source, include_segments=retain)

    def test_legacy_buckets_remain_unchanged_without_segments(self):
        source, _ = fixture()
        source["turns"][0].pop("usageSegments")
        for retain in (True, False):
            self.assertEqual(reproject_usage(source, include_segments=retain), source)

    def test_large_observation_keeps_every_increment_and_provenance(self):
        source, when = fixture()
        turn = _Turn("large-turn", "fixture-thread", when)
        record = source["turns"][0]["usageSegments"][0]
        for index in range(5000):
            turn.add(record["tokens"], when + index,
                     metadata={key: record[key] for key in ("model", "serviceTier", "pricingMetadataStatus")},
                     api_context="short", provenance={"eventOrdinal": index, "eventPart": 0})
        source["turns"] = [turn.export()]
        full = reproject_usage(source)
        self.assertEqual(full, original_projection(source))
        light = reproject_usage(source, include_segments=False)
        self.assertEqual(light["turns"][0]["tokens"]["total"], 500_000)
        self.assertEqual(full["turns"][0]["usageSegments"], source["turns"][0]["usageSegments"])


if __name__ == "__main__":
    unittest.main()
