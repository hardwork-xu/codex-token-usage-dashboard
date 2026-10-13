"""Synthetic full-history and stable-pagination regressions."""
import base64
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.subject = meter.Meter(self.folder)
        self.logs = {"older-task": [self.turn("older-task", i, i) for i in range(1, 52)],
                     "recent-task": [self.turn("recent-task", i, 1000 + i) for i in range(1, 206)]}
        meter.write_json(self.folder / "registry.json", {key: {"path": key} for key in self.logs})
        meter.write_json(self.folder / "settings.json", {"pricingMode": "custom", "ratePerMillion": "1"})
        patch = mock.patch.object(meter.UsageLogReader, "read", side_effect=lambda path, **kw: {
            "turns": self.logs[str(path)], "reading": {"complete": True}})
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def turn(thread, number, when):
        return {"id": thread + ":" + str(number), "threadId": thread, "turnId": str(number),
                "startedAt": when, "endedAt": when + 1, "quality": "complete",
                "tokens": {"input": 90, "output": 10, "total": 100, "cachedInput": 0}}

    def test_selected_history_filters_before_global_cap(self):
        state = self.subject.snapshot()
        self.assertEqual(len([t for t in state["turns"] if t["threadId"] == "older-task"]), 1)
        self.assertEqual(state["history"]["total"], 256)
        history = self.subject.history("older-task")
        self.assertEqual(history["total"], 51)
        self.assertEqual([t["turnNumber"] for t in history["turns"]], list(range(51, 0, -1)))
        self.assertIsNone(history["nextCursor"])
        self.assertEqual(sum(t["tokens"]["total"] for t in history["turns"]), 5100)

    def test_cursor_survives_new_front_rows_and_timestamp_ties(self):
        self.logs["older-task"] = [self.turn("older-task", i, 10) for i in range(1, 8)]
        first = self.subject.history("older-task", limit=3)
        self.logs["older-task"].append(self.turn("older-task", 8, 11))
        second = self.subject.history("older-task", limit=3, cursor=first["nextCursor"])
        third = self.subject.history("older-task", limit=3, cursor=second["nextCursor"])
        ids = [t["id"] for page in (first, second, third) for t in page["turns"]]
        self.assertEqual(len(ids), 7)
        self.assertEqual(len(set(ids)), 7)
        self.assertEqual(set(ids), {t["id"] for t in self.logs["older-task"][:-1]})
        self.assertIsNone(third["nextCursor"])
        self.assertEqual(second["total"], 8)

    def test_all_task_pages_conserve_tokens_and_prices_recompute(self):
        records, cursor = [], None
        while True:
            page = self.subject.history(limit=100, cursor=cursor)
            records.extend(page["turns"])
            cursor = page["nextCursor"]
            if cursor is None:
                break
        self.assertEqual(len(records), 256)
        self.assertEqual(len({t["id"] for t in records}), 256)
        self.assertEqual(sum(t["tokens"]["total"] for t in records), 25600)
        before = self.subject.history("older-task")["turns"][0]["amount"]
        meter.write_json(self.folder / "settings.json", {"pricingMode": "custom", "ratePerMillion": "2"})
        after = self.subject.history("older-task")["turns"][0]["amount"]
        self.assertEqual(meter.decimal.Decimal(after), 2 * meter.decimal.Decimal(before))

    def test_deleted_or_unreadable_log_cannot_return_cached_history(self):
        self.assertEqual(self.subject.history("older-task")["total"], 51)
        with mock.patch.object(meter.UsageLogReader, "read", side_effect=OSError()):
            self.assertEqual(self.subject.history("older-task")["total"], 0)
        meter.write_json(self.folder / "registry.json", {})
        with self.assertRaises(ValueError):
            self.subject.history("older-task")

    def test_bounded_validated_cursor_and_scope(self):
        first = self.subject.history("older-task", limit=1)
        for args in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"thread_id": ""},
                     {"thread_id": "not-registered"}, {"cursor": "?"}, {"cursor": "x" * 1025},
                     {"thread_id": "recent-task", "cursor": first["nextCursor"]}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.subject.history(**args)
        for payload in (None, [], {"version": 1, "key": [float("nan"), "a", "b"]},
                        {"version": 1, "key": [1, "a"]}):
            cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.subject.history(cursor=cursor)


class HistoryEndpointTests(unittest.TestCase):
    def get(self, path, allowed=True):
        handler = meter.Handler.__new__(meter.Handler)
        handler.server = SimpleNamespace(meter=SimpleNamespace(history=mock.Mock(return_value={"turns": [], "total": 0, "nextCursor": None})))
        handler.path = path
        handler.allowed = mock.Mock(return_value=allowed)
        handler.respond = mock.Mock()
        handler.do_GET()
        return handler

    def test_history_remains_same_origin(self):
        handler = self.get("/api/history", False)
        self.assertEqual(handler.respond.call_args.args[1], 403)
        handler.server.meter.history.assert_not_called()

    def test_query_is_strict_bounded_and_passed_to_reader(self):
        handler = self.get("/api/history?threadId=fixture&limit=100&cursor=opaque")
        handler.server.meter.history.assert_called_once_with("fixture", 100, "opaque")
        for path in ("/api/history?foo=1", "/api/history?limit=1&limit=2", "/api/history?limit=abc",
                     "/api/history?limit=", "/api/history?cursor=" + "a" * 2100,
                     "/api/history?threadId=a&limit=1&cursor=b&foo=1"):
            with self.subTest(path=path[:50]):
                handler = self.get(path)
                self.assertEqual(handler.respond.call_args.args[1], 400)
                handler.server.meter.history.assert_not_called()


if __name__ == "__main__":
    unittest.main()
