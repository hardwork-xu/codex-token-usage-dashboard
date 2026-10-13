"""Synthetic service integration for incremental history and live appends."""
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import meter
import usage_log


def record(kind, **payload):
    return {'type': 'event_msg', 'payload': {'type': kind, **payload}}


def count(total):
    amount = dict(total_tokens=total, input_tokens=total, cached_input_tokens=0,
                  cache_write_input_tokens=0, output_tokens=0, reasoning_output_tokens=0)
    return record('token_count', info={'total_token_usage': amount, 'last_token_usage': amount})


class IncrementalMeterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'synthetic.jsonl'
        self.records = [{'type': 'session_meta', 'payload': {'id': 'synthetic-thread'}},
                        record('task_started', turn_id='synthetic-turn'), count(10)]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.records), encoding='utf-8')
        meter.write_json(self.folder / 'registry.json', {'synthetic-thread': {'path': str(self.path)}})
        self.subject = meter.Meter(self.folder)

    def append(self, rows):
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(''.join(json.dumps(row) + '\n' for row in rows))

    def test_history_progress_becomes_live_without_recounting(self):
        self.append([{'type': 'response_item', 'payload': {'text': 'synthetic padding'}}] * 80)
        factory = lambda thread, **options: usage_log.UsageLogReader(thread, byte_budget=4096, **options)
        with mock.patch.object(usage_log, 'MAX_LINE_BYTES', 1024), \
                mock.patch.object(meter, 'UsageLogReader', side_effect=factory):
            states = [self.subject.snapshot()]
            for _ in range(20):
                if states[-1]['conversations'][0]['reading']['complete']:
                    break
                states.append(self.subject.snapshot())
            self.assertFalse(states[0]['conversations'][0]['reading']['complete'])
            self.assertEqual(states[0]['conversations'][0]['activeTurnCount'], 0)
            self.assertTrue(states[-1]['conversations'][0]['reading']['complete'])
            self.assertEqual(states[-1]['turns'][0]['tokens']['total'], 10)
            self.append([count(20)])
            latest = self.subject.snapshot()
            self.assertEqual(latest['turns'][0]['tokens']['total'], 20)
            self.assertEqual(self.subject.snapshot()['turns'][0]['tokens']['total'], 20)

    def test_foreign_session_never_revives_a_previously_valid_prefix(self):
        self.assertEqual(self.subject.snapshot()['turns'][0]['tokens']['total'], 10)
        self.append([{'type': 'session_meta', 'payload': {'id': 'foreign-thread'}}])
        for _ in range(3):
            state = self.subject.snapshot()
            self.assertEqual(state['turns'], [])
            self.assertIsNotNone(state['conversations'][0]['readError'])
        self.append([count(20)])
        self.assertEqual(self.subject.snapshot()['turns'], [])

    def test_corrupt_registry_does_not_report_zero_spending(self):
        for content in ('not-json', 'null', '[]'):
            with self.subTest(content=content):
                (self.folder / 'registry.json').write_text(content, encoding='utf-8')
                state = self.subject.snapshot()
                self.assertEqual(state['monitoring']['status'], 'partial')
                self.assertIsNone(state['periods']['today']['tokens'])
                self.assertIsNone(state['periods']['today']['amount'])

    def test_historical_warning_does_not_hide_successful_refresh_time(self):
        self.append([record('context_compacted')])
        state = self.subject.snapshot()
        self.assertEqual(state['monitoring']['status'], 'partial')
        self.assertIsNotNone(state['monitoring']['lastUpdate'])

    def test_history_cap_preserves_a_record_for_an_older_selected_task(self):
        meter.write_json(self.folder / 'registry.json', {
            'synthetic-thread': {'path': str(self.path)}, 'older-thread': {'path': str(self.path)}})
        fresh = [{'id': str(i), 'threadId': 'synthetic-thread', 'startedAt': i + 1,
                  'tokens': None, 'quality': 'unavailable'} for i in range(250)]
        older = {'id': 'old', 'threadId': 'older-thread', 'startedAt': 0, 'tokens': None, 'quality': 'unavailable'}
        with mock.patch.object(meter.UsageLogReader, 'read', side_effect=[
                {'turns': fresh, 'warnings': []}, {'turns': [older], 'warnings': []}]):
            state = self.subject.snapshot()
        self.assertEqual(len(state['turns']), 201)
        self.assertEqual(state['turns'][-1]['threadId'], 'older-thread')
        self.assertEqual(sum(c['turnCount'] for c in state['conversations']), 251)

    def test_snapshot_requests_serialize_reader_creation_and_advance(self):
        real = usage_log.UsageLogReader('synthetic-thread')
        active = threading.Lock()
        def read(path, **kwargs):
            self.assertTrue(active.acquire(blocking=False), 'Overlapping reader use')
            try:
                time.sleep(0.01)
                return real.read(path, **kwargs)
            finally:
                active.release()
        reader = mock.Mock(read=mock.Mock(side_effect=read))
        with mock.patch.object(meter, 'UsageLogReader', return_value=reader) as factory:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                states = list(pool.map(lambda _: self.subject.snapshot(), range(4)))
            factory.assert_called_once_with('synthetic-thread', override_windows={})
        self.assertEqual([state['turns'][0]['tokens']['total'] for state in states], [10] * 4)


if __name__ == '__main__':
    unittest.main()
