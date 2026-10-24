"""Frozen allocation through the service and same-origin POST, synthetic only."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
from device_allocation import validate_allocation
from test_device_allocation import conversation, turn, SETTINGS
from test_headless_collection import THREAD, records, event, context, usage


class DeviceAllocationIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.path = self.folder / "synthetic.jsonl"
        self.report_path = self.folder / "device-allocation.json"
        self.path.write_text("".join(json.dumps(row) + "\n" for row in records()), encoding="utf-8")
        meter.write_json(self.folder / "registry.json", {THREAD: {"path": str(self.path)}})
        meter.write_json(self.folder / "settings.json", {"subscriptionRenewalDay": 9, "pricingMode": "api"})
        self.subject = meter.Meter(self.folder)

    def test_real_registered_source_archive_and_snapshot_to_frozen_report(self):
        state = self.subject.snapshot()
        self.assertEqual(state["turns"][0]["tokens"]["total"], 100)
        self.assertIsNone(state["deviceAllocation"])
        self.assertIsNone(state["deviceAllocationError"])
        result = self.subject.record_device_allocation({})
        self.assertTrue(result["ok"])
        report = result["deviceAllocation"]
        self.assertEqual(report["periods"]["allTime"]["local"]["tokens"]["total"], 100)
        self.assertEqual(validate_allocation(json.loads(self.report_path.read_text())), report)
        self.assertEqual(self.subject.archive.load(THREAD)["result"]["turns"][0]["tokens"]["total"], 100)
        encoded = self.report_path.read_text()
        for forbidden in (THREAD, str(self.path), "threadId", "accountId", "csrf", "sourceAvailability"):
            self.assertNotIn(forbidden, encoded)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.report_path.stat().st_mode), 0o600)

    def test_frozen_report_does_not_append_later_usage_or_rebuild_during_get(self):
        report = self.subject.record_device_allocation({})["deviceAllocation"]
        saved = self.report_path.read_bytes()
        later = [event("task_started", turn_id="next"),
                 {"type": "turn_context", "payload": {"turn_id": "next", "model": "gpt-6-luna", "service_tier": "default"}},
                 usage(300, 200), event("task_complete", turn_id="next")]
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(row) + "\n" for row in later))
        with mock.patch.object(meter, "build_allocation", side_effect=AssertionError("GET must not reclassify")):
            state = self.subject.snapshot()
        self.assertEqual(sum(row["tokens"]["total"] for row in state["turns"]), 300)
        self.assertEqual(state["deviceAllocation"], report)
        self.assertEqual(self.report_path.read_bytes(), saved)

    def test_complete_history_is_used_instead_of_visible_200_rows(self):
        values = [turn(ident=str(index)) for index in range(251)]
        fake = {"turns": values[:200], "conversations": [conversation()], "settings": SETTINGS,
                "monitoring": {"activeStatus": "active", "archive": {"writeErrors": 0},
                               "collection": {"writeErrors": 0}, "coverage": {"backfillingTasks": 0}}}
        self.subject.history_turns = values
        with mock.patch.object(self.subject, "_snapshot_for_generation", return_value=fake) as snapshot:
            result = self.subject.record_device_allocation({})
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(result["deviceAllocation"]["periods"]["allTime"]["total"]["tokens"]["total"], 251 * 1100)

    def test_new_install_does_not_report_missing_snapshot_as_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            subject = meter.Meter(Path(temporary).resolve())
            self.assertEqual(subject.device_allocation_view(), (None, None))
            snapshot = subject.snapshot()
            self.assertIsNone(snapshot["deviceAllocation"])
            self.assertIsNone(snapshot["deviceAllocationError"])

    def test_invalid_or_oversized_snapshot_does_not_change_real_usage(self):
        for raw in ("not-json", "null", "{}", "x" * 16385):
            self.report_path.write_text(raw, encoding="utf-8")
            state = self.subject.snapshot()
            self.assertIsNone(state["deviceAllocation"])
            self.assertTrue(state["deviceAllocationError"])
            self.assertEqual(state["turns"][0]["tokens"]["total"], 100)

    def test_linked_metadata_is_not_read_and_does_not_change_source_usage(self):
        target = self.folder / "private-target.json"
        target.write_text("PRIVATE_CONTENT", encoding="utf-8")
        try:
            self.report_path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        state = self.subject.snapshot()
        self.assertIsNone(state["deviceAllocation"])
        self.assertTrue(state["deviceAllocationError"])
        self.assertNotIn("PRIVATE_CONTENT", state["deviceAllocationError"])
        self.assertEqual(target.read_text(), "PRIVATE_CONTENT")
        self.assertEqual(state["turns"][0]["tokens"]["total"], 100)

    def test_current_errors_or_backfill_preserve_existing_report(self):
        self.subject.record_device_allocation({})
        saved = self.report_path.read_bytes()
        fake = self.subject.snapshot()
        changes = [("archive", "writeErrors", 1), ("collection", "writeErrors", 1),
                   ("coverage", "backfillingTasks", 1)]
        for section, key, value in changes:
            state = deepcopy(fake); state["monitoring"][section][key] = value
            with self.subTest(section=section), mock.patch.object(self.subject, "_snapshot_for_generation", return_value=state), \
                    self.assertRaises(ValueError):
                self.subject.record_device_allocation({})
            self.assertEqual(self.report_path.read_bytes(), saved)
        state = deepcopy(fake); state["conversations"][0]["readErrorCode"] = "source_unreadable"
        state["conversations"][0]["readError"] = "synthetic denied"
        with mock.patch.object(self.subject, "_snapshot_for_generation", return_value=state), self.assertRaises(ValueError):
            self.subject.record_device_allocation({})
        self.assertEqual(self.report_path.read_bytes(), saved)

    def test_archive_write_failure_and_missing_active_source_cannot_overwrite_snapshot(self):
        self.subject.record_device_allocation({})
        saved = self.report_path.read_bytes()
        with mock.patch.object(self.subject.archive, "save", side_effect=OSError("synthetic disk failure")):
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaises(ValueError):
                self.subject.record_device_allocation({})
        self.assertEqual(self.report_path.read_bytes(), saved)
        self.path.unlink()
        with self.assertRaises(ValueError):
            self.subject.record_device_allocation({})
        self.assertEqual(self.report_path.read_bytes(), saved)

    def test_acknowledged_missing_source_is_classified_as_residual_once(self):
        self.subject.snapshot()
        self.path.unlink()
        self.subject.acknowledge_historical_gaps({"threadIds": [THREAD]})
        report = self.subject.record_device_allocation({})["deviceAllocation"]
        all_time = report["periods"]["allTime"]
        self.assertEqual(all_time["total"]["tokens"]["total"], 100)
        self.assertEqual(all_time["other"]["tokens"]["total"], 100)
        self.assertEqual(all_time["local"]["tokens"]["total"], 0)
        self.assertEqual(report["unknownTaskCount"], 0)

    def test_only_empty_object_is_accepted_without_triggering_scan(self):
        for value in (None, [], True, 1, "{}", {"tokens": 100}, {"amount": "1"}, {"accountId": "private"}):
            with self.subTest(value=value), mock.patch.object(self.subject, "_snapshot_for_generation") as scan, self.assertRaises(ValueError):
                self.subject.record_device_allocation(value)
            scan.assert_not_called()


class DeviceAllocationEndpointTests(unittest.TestCase):
    def request(self, value, *, token="fixture-token", origin=None, content_type="application/json"):
        handler = meter.Handler.__new__(meter.Handler)
        subject = SimpleNamespace(csrf="fixture-token", record_device_allocation=mock.Mock(return_value={"ok": True}))
        handler.server = SimpleNamespace(server_port=43210, meter=subject)
        raw = json.dumps(value).encode()
        handler.path = "/api/device-allocation"
        handler.headers = {"Host": "127.0.0.1:43210", "X-Meter-Token": token,
                           "Content-Type": content_type, "Content-Length": str(len(raw))}
        if origin:
            handler.headers["Origin"] = origin
        handler.rfile = io.BytesIO(raw)
        handler.respond = mock.Mock()
        handler.do_POST()
        return handler

    def test_same_origin_and_csrf_remain_required(self):
        for options in ({"token": ""}, {"token": "wrong"}, {"origin": "https://other.invalid"}):
            handler = self.request({}, **options)
            self.assertEqual(handler.respond.call_args.args[1], 403)
            handler.server.meter.record_device_allocation.assert_not_called()

    def test_valid_post_forwards_empty_object_and_rejects_wrong_content_type(self):
        handler = self.request({})
        handler.server.meter.record_device_allocation.assert_called_once_with({})
        self.assertEqual(handler.respond.call_args.args[0], {"ok": True})
        handler = self.request({}, content_type="text/plain")
        self.assertEqual(handler.respond.call_args.args[1], 400)
        handler.server.meter.record_device_allocation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
