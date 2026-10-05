"""Synthetic recovery scheduling and registry integrity; no real Codex access."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import meter


class DescendantIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.meter = meter.Meter(self.folder)
        self.meter.descendant_roots = {'root': 100}
        meter.write_json(self.folder / 'registry.json', {'root': {'path': 'root.jsonl'}})
        self.command = patch.object(meter, 'codex_command', return_value=['synthetic-codex'])
        self.command.start()
        self.addCleanup(self.command.stop)

    def response(self, **extra):
        return {'threads': [], 'complete': True, 'nextCursor': None,
                'rejected': 0, 'deferred': 0, 'error': None, **extra}

    def test_verified_result_merges_without_overwriting_hook_or_alias(self):
        def reply(records, root, command, **options):
            meter.write_json(self.folder / 'registry.json', {
                'root': {'path': 'root.jsonl', 'alias': 'saved title'},
                'existing': {'path': 'hook.jsonl'}})
            return self.response(threads=[{'threadId': 'existing', 'path': 'other.jsonl'},
                                         {'threadId': 'child', 'path': 'child.jsonl'}])
        with patch.object(meter, 'discover_descendants', side_effect=reply):
            self.meter.refresh_descendants()
        records = meter.read_json(self.folder / 'registry.json', {})
        self.assertEqual(records['root']['alias'], 'saved title')
        self.assertEqual(records['existing']['path'], 'hook.jsonl')
        self.assertEqual(records['child']['registrationSource'], 'verified_descendant')
        self.assertEqual(self.meter.descendant_status['status'], 'checked')

    def test_cooldown_deduplicates_and_archive_pass_follows_active(self):
        with patch.object(meter, 'discover_descendants', return_value=self.response()) as rpc:
            self.meter.refresh_descendants()
            self.meter.refresh_descendants()
            self.assertEqual(rpc.call_count, 1)
            self.assertFalse(rpc.call_args.kwargs['archived'])
            self.meter.last_descendant_attempt = 0
            self.meter.refresh_descendants()
            self.assertTrue(rpc.call_args.kwargs['archived'])

    def test_pagination_resumes_and_error_preserves_cursor(self):
        self.meter.descendant_progress['root'] = {'cursor': 'page-two', 'archived': False}
        with patch.object(meter, 'discover_descendants', return_value=self.response(error='unavailable', nextCursor='page-two')) as rpc:
            self.meter.refresh_descendants()
        self.assertEqual(rpc.call_args.kwargs['cursor'], 'page-two')
        self.assertEqual(self.meter.descendant_progress['root']['cursor'], 'page-two')
        self.assertEqual(self.meter.descendant_status['status'], 'partial')

    def test_later_page_failure_retains_earlier_verified_rows_and_progress(self):
        response = self.response(error='unavailable', complete=False, nextCursor='failed-page',
                                 threads=[{'threadId': 'child', 'path': 'child.jsonl'}])
        with patch.object(meter, 'discover_descendants', return_value=response):
            self.meter.refresh_descendants()
        self.assertEqual(self.meter.descendant_progress['root']['cursor'], 'failed-page')
        self.assertFalse(self.meter.descendant_progress['root']['archived'])
        self.assertIn('child', meter.read_json(self.folder / 'registry.json', {}))

    def test_missing_root_and_locked_worker_do_not_query(self):
        with patch.object(meter, 'discover_descendants') as rpc:
            self.meter.descendant_lock.acquire()
            self.meter.refresh_descendants()
            self.meter.descendant_lock.release()
            self.meter.descendant_roots = {}
            self.meter.refresh_descendants()
            rpc.assert_not_called()

    def test_failures_are_content_free_and_release_worker_lock(self):
        with patch.object(meter, 'discover_descendants', side_effect=RuntimeError('private path')):
            self.meter.refresh_descendants()
        self.assertEqual(self.meter.descendant_status['status'], 'unavailable')
        self.assertNotIn('private', json.dumps(self.meter.descendant_status))
        self.assertFalse(self.meter.descendant_lock.locked())

    def test_two_roots_per_batch_rotates_to_unchecked_roots(self):
        roots = {f'root{i}': {'path': f'root{i}.jsonl'} for i in range(4)}
        meter.write_json(self.folder / 'registry.json', roots)
        self.meter.descendant_roots = {key: index for index, key in enumerate(roots)}
        with patch.object(meter, 'discover_descendants', return_value=self.response()) as rpc:
            self.meter.refresh_descendants()
            self.meter.last_descendant_attempt = 0
            self.meter.refresh_descendants()
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ['root3', 'root2', 'root1', 'root0'])
