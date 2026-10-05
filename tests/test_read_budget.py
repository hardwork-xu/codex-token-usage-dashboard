"""Fair soft parsing deadlines over synthetic files; no real logs or RPCs."""
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
import usage_log


def records(thread="budget-fixture", total=100):
    amount = dict(total_tokens=total, input_tokens=total, cached_input_tokens=0,
                  cache_write_input_tokens=0, output_tokens=0, reasoning_output_tokens=0)
    return [
        {"type": "session_meta", "payload": {"id": thread}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "synthetic-turn"}},
        {"type": "turn_context", "payload": {"turn_id": "synthetic-turn", "model": "gpt-6-astra"}},
        {"type": "event_msg", "timestamp": "2026-09-21T12:00:00+00:00", "payload": {
            "type": "token_count", "info": {"total_token_usage": amount, "last_token_usage": amount}}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "synthetic-turn"}},
    ]


def encode(rows):
    return b"".join((json.dumps(row) + "\n").encode("utf-8") for row in rows)


class AdvancingClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        self.now += 2
        return self.now


class ReaderBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="read-budget-fixture-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "synthetic.jsonl"
        self.rows = records()
        self.path.write_bytes(encode(self.rows))

    def test_invalid_budget_fails_before_any_file_access(self):
        reader = usage_log.UsageLogReader("budget-fixture")
        invalid = (None, True, False, 0, -0.1, 1.00001, float("inf"), float("-inf"),
                   float("nan"), "0.1", {}, [], 10**1000)
        with mock.patch.object(Path, "lstat", side_effect=AssertionError("Must validate before opening")) as stat:
            for budget in invalid:
                with self.subTest(budget=repr(budget)[:30]), self.assertRaises(ValueError):
                    reader.read(self.path, time_budget=budget)
            stat.assert_not_called()

    def test_default_budget_matches_explicit_one_second(self):
        with mock.patch.object(usage_log.time, "monotonic", return_value=0):
            implicit = usage_log.UsageLogReader("budget-fixture").read(self.path)
            explicit = usage_log.UsageLogReader("budget-fixture").read(self.path, time_budget=1.0)
        self.assertEqual(implicit, explicit)
        self.assertTrue(explicit["reading"]["complete"])

    def test_smallest_positive_budget_still_advances_one_record(self):
        reader = usage_log.UsageLogReader("budget-fixture")
        previous = 0
        with mock.patch.object(usage_log.time, "monotonic", side_effect=AdvancingClock()):
            for index in range(len(self.rows)):
                result = reader.read(self.path, time_budget=float.fromhex("0x0.0000000000001p-1022"))
                self.assertGreater(result["reading"]["bytesRead"], previous)
                previous = result["reading"]["bytesRead"]
                self.assertEqual(previous, len(encode(self.rows[:index + 1])))
        self.assertTrue(result["reading"]["complete"])
        self.assertEqual(result["turns"][0]["tokens"]["total"], 100)
        self.assertEqual(result["turns"][0]["quality"], "complete")

    def test_deadline_yields_between_records_without_losing_baseline(self):
        reader = usage_log.UsageLogReader("budget-fixture")
        clock = [0.0]
        original = usage_log._Reader.handle
        def handle(subject, row):
            original(subject, row)
            clock[0] += .02
        with mock.patch.object(usage_log.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(usage_log._Reader, "handle", handle):
            first = reader.read(self.path, time_budget=.05)
            self.assertFalse(first["reading"]["complete"])
            self.assertEqual(first["reading"]["bytesRead"], len(encode(self.rows[:3])))
            complete = reader.read(self.path, time_budget=1)
        self.assertTrue(complete["reading"]["complete"])
        self.assertEqual(complete["turns"][0]["tokens"]["total"], 100)
        self.assertIsNone(first["turns"][0]["tokens"])

    def test_budget_changes_do_not_clear_foreign_identity_rejection(self):
        reader = usage_log.UsageLogReader("budget-fixture")
        self.assertEqual(reader.read(self.path)["turns"][0]["tokens"]["total"], 100)
        with self.path.open("ab") as stream:
            stream.write(encode([{"type": "session_meta", "payload": {"id": "foreign-fixture"}}]))
        for budget in (1e-15, 1.0):
            with self.subTest(budget=budget), self.assertRaises(usage_log.UsageLogError):
                reader.read(self.path, time_budget=budget)

    def test_tiny_budget_still_checks_replacement_identity(self):
        reader = usage_log.UsageLogReader("budget-fixture")
        reader.read(self.path)
        replacement = self.folder / "replacement.jsonl"
        replacement.write_bytes(encode(records("foreign-fixture")))
        replacement.replace(self.path)
        with self.assertRaises(usage_log.UsageLogError):
            reader.read(self.path, time_budget=1e-15)


class SnapshotBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-budget-fixture-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        for name in ("fetch_quota", "fetch_conversation_titles"):
            patch = mock.patch.object(meter, name, side_effect=AssertionError("No live RPCs in budget tests"))
            patch.start()
            self.addCleanup(patch.stop)
        child = mock.patch.object(meter.subprocess, "Popen", side_effect=AssertionError("No live child process"))
        child.start()
        self.addCleanup(child.stop)

    def seed(self, count):
        registry = {}
        for index in range(count):
            thread = f"synthetic-budget-thread-{index}"
            path = self.folder / f"synthetic-{index}.jsonl"
            path.write_bytes(encode(records(thread, (index + 1) * 100)))
            registry[thread] = {"path": str(path), "registeredAt": index + 1}
        meter.write_json(self.folder / "registry.json", registry)
        return registry

    def test_every_registered_reader_receives_an_equal_share(self):
        for count in (1, 3, 137):
            with self.subTest(count=count):
                registry = self.seed(count)
                subject = meter.Meter(self.folder)
                calls = []
                def factory(thread):
                    def read(path, *, time_budget):
                        calls.append((thread, str(path), time_budget))
                        return {"turns": [], "warnings": [], "reading": {"complete": True, "bytesRead": 0, "fileBytes": 0}}
                    return mock.Mock(read=read)
                with mock.patch.object(meter, "UsageLogReader", side_effect=factory):
                    result = subject.snapshot()
                self.assertEqual({call[0] for call in calls}, set(registry))
                self.assertEqual(len(calls), count)
                self.assertAlmostEqual(sum(call[2] for call in calls), 1.0)
                for thread, path, budget in calls:
                    self.assertEqual(path, registry[thread]["path"])
                    self.assertEqual(budget, min(1.0, 1.0 / count))
                self.assertTrue(all(row["readError"] is None for row in result["conversations"]))

    def test_each_task_advances_and_period_totals_converge_without_omissions(self):
        registry = self.seed(7)
        subject = meter.Meter(self.folder)
        # The UTC event can map to either adjacent local day on different hosts;
        # choose the actual local bucket date, then inject it into aggregation.
        from datetime import datetime
        event_day = datetime.fromisoformat("2026-09-21T12:00:00+00:00").astimezone().date()
        aggregate = partial(meter.summarize_periods, now=event_day)
        previous = dict.fromkeys(registry, 0)
        with mock.patch.object(usage_log.time, "monotonic", side_effect=AdvancingClock()), \
                mock.patch.object(meter, "summarize_periods", side_effect=aggregate):
            for _ in range(5):
                state = subject.snapshot()
                indexed = {row["id"]: row for row in state["conversations"]}
                self.assertEqual(set(indexed), set(registry))
                for thread, row in indexed.items():
                    self.assertIsNone(row["readError"])
                    self.assertGreater(row["reading"]["bytesRead"], previous[thread])
                    previous[thread] = row["reading"]["bytesRead"]
            again = subject.snapshot()
        self.assertTrue(all(row["reading"]["complete"] for row in state["conversations"]))
        expected = sum((index + 1) * 100 for index in range(7))
        self.assertEqual(sum(turn["tokens"]["total"] for turn in state["turns"]), expected)
        self.assertEqual(state["periods"]["today"]["tokens"]["total"], expected)
        self.assertEqual(state["periods"]["today"], again["periods"]["today"])
        self.assertFalse(state["periods"]["today"]["partial"])

    def test_pending_history_receives_most_budget_after_first_pass(self):
        registry = self.seed(51)
        pending = next(iter(registry))
        subject = meter.Meter(self.folder)
        phase, calls = [0], []
        def factory(thread):
            real = usage_log.UsageLogReader(thread)
            def read(path, *, time_budget):
                calls.append((thread, time_budget))
                result = real.read(path, time_budget=time_budget)
                if thread == pending and phase[0] < 2:
                    result["reading"]["complete"] = False
                return result
            return mock.Mock(read=read)
        from datetime import datetime
        event_day = datetime.fromisoformat("2026-09-21T12:00:00+00:00").astimezone().date()
        aggregate = partial(meter.summarize_periods, now=event_day)
        expected = sum((index + 1) * 100 for index in range(51))
        # This test checks allocation, while ReaderBudgetTests exercises real
        # deadline yields with an advancing clock. Host scheduling must not
        # decide whether these five-record fixtures finish their first pass.
        with mock.patch.object(meter, "UsageLogReader", side_effect=factory), \
                mock.patch.object(usage_log.time, "monotonic", return_value=0), \
                mock.patch.object(meter, "summarize_periods", side_effect=aggregate):
            for step in range(4):
                phase[0] = step
                calls.clear()
                state = subject.snapshot()
                shares = dict(calls)
                self.assertEqual(len(calls), len(registry))
                self.assertEqual(set(shares), set(registry))
                self.assertAlmostEqual(sum(shares.values()), 1.0)
                if step in (1, 2):
                    self.assertGreater(shares[pending], .5)
                    self.assertAlmostEqual(shares[pending], 100 / 150)
                    self.assertTrue(all(abs(budget - 1 / 150) < 1e-12
                                        for thread, budget in calls if thread != pending))
                else:
                    self.assertTrue(all(abs(budget - 1 / 51) < 1e-12 for budget in shares.values()))
                self.assertEqual(sum(turn["tokens"]["total"] for turn in state["turns"]), expected)
                self.assertEqual(state["periods"]["today"]["tokens"]["total"], expected)
        self.assertTrue(all(type(value) is bool for value in subject.reader_incomplete.values()))
        self.assertFalse(any(subject.reader_incomplete.values()))

    def test_completed_file_growth_is_read_and_regains_priority(self):
        registry = self.seed(3)
        changed = next(iter(registry))
        subject = meter.Meter(self.folder)
        calls = []
        def factory(thread):
            real = usage_log.UsageLogReader(thread)
            def read(path, *, time_budget):
                calls.append((thread, time_budget))
                return real.read(path, time_budget=time_budget)
            return mock.Mock(read=read)
        with mock.patch.object(meter, "UsageLogReader", side_effect=factory):
            first = subject.snapshot()
            self.assertEqual(sum(turn["tokens"]["total"] for turn in first["turns"]), 600)
            self.assertFalse(any(subject.reader_incomplete.values()))
            appended = records(changed, 150)[1:]
            for row in appended:
                if "turn_id" in row["payload"]:
                    row["payload"]["turn_id"] = "synthetic-next-turn"
            with Path(registry[changed]["path"]).open("ab") as stream:
                stream.write(encode(appended))
            with mock.patch.object(usage_log.time, "monotonic", side_effect=AdvancingClock()):
                for step in range(4):
                    calls.clear()
                    state = subject.snapshot()
                    shares = dict(calls)
                    self.assertEqual(set(shares), set(registry))
                    self.assertEqual(len(calls), len(registry))
                    self.assertAlmostEqual(sum(shares.values()), 1.0)
                    self.assertAlmostEqual(shares[changed], 1 / 3 if step == 0 else 100 / 102)
                    if step == 0:
                        self.assertTrue(subject.reader_incomplete[changed])
        self.assertFalse(any(subject.reader_incomplete.values()))
        self.assertEqual(sum(turn["tokens"]["total"] for turn in state["turns"]), 650)

    def test_failed_read_stays_pending_and_removed_ids_are_pruned(self):
        registry = self.seed(3)
        broken = next(iter(registry))
        subject = meter.Meter(self.folder)
        fail, calls = [False], []
        def factory(thread):
            real = usage_log.UsageLogReader(thread)
            def read(path, *, time_budget):
                calls.append((thread, time_budget))
                if fail[0] and thread == broken:
                    raise usage_log.UsageLogError("Synthetic unavailable file")
                return real.read(path, time_budget=time_budget)
            return mock.Mock(read=read)
        with mock.patch.object(meter, "UsageLogReader", side_effect=factory):
            subject.snapshot()
            fail[0] = True
            subject.snapshot()
            self.assertTrue(subject.reader_incomplete[broken])
            calls.clear()
            failed = subject.snapshot()
            self.assertAlmostEqual(dict(calls)[broken], 100 / 102)
            self.assertFalse(any(turn["threadId"] == broken for turn in failed["turns"]))
            del registry[broken]
            meter.write_json(self.folder / "registry.json", registry)
            calls.clear()
            subject.snapshot()
        self.assertNotIn(broken, subject.cache)
        self.assertNotIn(broken, subject.reader_incomplete)
        self.assertEqual(set(dict(calls)), set(registry))
        self.assertTrue(all(budget == .5 for _, budget in calls))


if __name__ == "__main__":
    unittest.main()
