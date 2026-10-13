"""Date speed declarations retain portable, explicit UTC boundaries."""
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


def window(day, offset=180, hours=24):
    start = (date.fromisoformat(day) - date(1970, 1, 1)).days * 86400 - offset * 60
    return {"startAt": start, "endAt": start + hours * 3600, "timeZone": "Fixture/Timezone"}


class SpeedWindowSettingsTests(unittest.TestCase):
    def setUp(self):
        self.day = "2026-10-05"
        self.settings = {"speedOverrides": {self.day: "fast"}, "speedOverrideWindows": {self.day: window(self.day)}}

    def test_windows_round_trip_without_mutating_or_guessing_legacy_zones(self):
        original = copy.deepcopy(self.settings)
        result = meter.validate_settings(self.settings)
        self.assertEqual(result["speedOverrideWindows"], self.settings["speedOverrideWindows"])
        result["speedOverrideWindows"][self.day]["startAt"] = 0
        self.assertEqual(self.settings, original)
        self.assertEqual(meter.validate_settings({"speedOverrides": {self.day: "fast"}})["speedOverrideWindows"], {})

    def test_real_dst_day_lengths_and_adjacent_windows_are_allowed(self):
        for hours in (23, 24, 25):
            self.settings["speedOverrideWindows"][self.day] = window(self.day, hours=hours)
            self.assertEqual(meter.validate_settings(self.settings)["speedOverrideWindows"], self.settings["speedOverrideWindows"])
        self.settings["speedOverrideWindows"][self.day] = window(self.day)
        self.settings["speedOverrides"]["2026-10-06"] = "standard"
        self.settings["speedOverrideWindows"]["2026-10-06"] = window("2026-10-06")
        self.assertEqual(len(meter.validate_settings(self.settings)["speedOverrideWindows"]), 2)

    def test_malformed_unrelated_and_overlapping_windows_are_rejected(self):
        good = window(self.day)
        bad = (None, [], {**good, "extra": 1}, {**good, "startAt": True},
               {**good, "endAt": good["startAt"]}, {**good, "endAt": good["endAt"] + 86400},
               {**good, "startAt": good["startAt"] - 86400, "endAt": good["endAt"] - 86400},
               {**good, "timeZone": "bad\nzone"}, {**good, "timeZone": ""},
               {**good, "timeZone": "x" * 129})
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                meter.validate_settings({**self.settings, "speedOverrideWindows": {self.day: value}})
        with self.assertRaises(ValueError):
            meter.validate_settings({**self.settings, "speedOverrides": {}})
        with self.assertRaises(ValueError):
            meter.validate_settings({"speedOverrides": {"1970-01-01": "fast"},
                                     "speedOverrideWindows": {"1970-01-01": window("1970-01-01", offset=480)}})
        with self.assertRaises(ValueError):
            meter.validate_settings({"speedOverrides": {self.day: "fast", "2026-10-06": "standard"},
                                     "speedOverrideWindows": {self.day: good, "2026-10-06": window("2026-10-06", offset=480)}})


class SpeedWindowPersistenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.subject = meter.Meter(self.folder)

    def post(self, value):
        body = json.dumps(value).encode()
        handler = meter.Handler.__new__(meter.Handler)
        handler.server = SimpleNamespace(meter=self.subject)
        handler.path = "/api/settings"
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.headers["Content-Type"] = "application/json"
        handler.rfile = io.BytesIO(body)
        handler.allowed = mock.Mock(return_value=True)
        handler.respond = mock.Mock()
        handler.do_POST()
        return handler.respond.call_args.args

    def test_old_client_currency_save_preserves_windows_but_date_removal_removes_them(self):
        day = "2026-10-05"
        source = {"pricingMode": "api", "speedOverrides": {day: "fast"}, "speedOverrideWindows": {day: window(day)}}
        meter.write_json(self.folder / "settings.json", source)
        self.assertTrue(self.post({"currencyCode": "HKD"})[0]["ok"])
        self.assertEqual(self.subject.settings()["speedOverrideWindows"], source["speedOverrideWindows"])
        self.assertTrue(self.post({"speedOverrides": {}})[0]["ok"])
        self.assertEqual(self.subject.settings()["speedOverrideWindows"], {})

    def test_full_year_of_windows_can_save_through_http(self):
        days = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(366)]
        result = self.post({"speedOverrides": {day: "fast" for day in days},
                            "speedOverrideWindows": {day: window(day) for day in days}})
        self.assertTrue(result[0]["ok"])
        self.assertEqual(len(self.subject.settings()["speedOverrideWindows"]), 366)

    def test_window_edit_invalidates_parsed_attribution(self):
        day = "2026-10-05"
        meter.write_json(self.folder / "registry.json", {"fixture": {"path": "fixture.jsonl"}})
        response = {"turns": [], "reading": {"complete": True}}
        with mock.patch.object(meter.UsageLogReader, "read", return_value=response):
            self.subject.snapshot()
            reader = self.subject.cache["fixture"]
            self.subject.snapshot()
            self.assertIs(self.subject.cache["fixture"], reader)
            self.post({"speedOverrides": {day: "fast"}, "speedOverrideWindows": {day: window(day)}})
            self.subject.snapshot()
            self.assertIsNot(self.subject.cache["fixture"], reader)


if __name__ == "__main__":
    unittest.main()
