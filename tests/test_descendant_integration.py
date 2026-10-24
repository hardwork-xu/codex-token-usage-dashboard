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

    def test_existing_child_verified_path_repairs_stale_registration_preserving_alias(self):
        meter.write_json(self.folder / 'registry.json', {
            'root': {'path': 'root.jsonl'},
            'child': {'path': 'stale.jsonl', 'alias': 'synthetic label', 'registeredAt': 123}})
        with patch.object(meter, 'discover_descendants', return_value=self.response(
                threads=[{'threadId': 'child', 'path': 'verified-new.jsonl'}])):
            self.meter.refresh_descendants()
        child = meter.read_json(self.folder / 'registry.json', {})['child']
        self.assertEqual(child, {'path': 'verified-new.jsonl', 'alias': 'synthetic label', 'registeredAt': 123})

    def test_existing_child_concurrent_hook_path_wins_over_metadata_result(self):
        meter.write_json(self.folder / 'registry.json', {
            'root': {'path': 'root.jsonl'}, 'child': {'path': 'stale.jsonl'}})
        def reply(*args, **kwargs):
            meter.write_json(self.folder / 'registry.json', {
                'root': {'path': 'root.jsonl'}, 'child': {'path': 'new-hook.jsonl', 'alias': 'live'}})
            return self.response(threads=[{'threadId': 'child', 'path': 'metadata.jsonl'}])
        with patch.object(meter, 'discover_descendants', side_effect=reply):
            self.meter.refresh_descendants()
        self.assertEqual(meter.read_json(self.folder / 'registry.json', {})['child'],
                         {'path': 'new-hook.jsonl', 'alias': 'live'})

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
        self.assertEqual(self.meter.descendant_status['status'], 'partial')
        self.assertEqual(self.meter.descendant_status['failedQueries'], 1)
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

    def test_cursor_archive_phase_and_roots_survive_worker_restart(self):
        response = self.response(complete=False, nextCursor='page-three')
        self.meter.descendant_progress['root'] = {'cursor': 'page-two', 'archived': True}
        with patch.object(meter, 'discover_descendants', return_value=response):
            self.meter.refresh_descendants()
        restarted = meter.Meter(self.folder)
        self.assertEqual(restarted.descendant_roots, {})
        with patch.object(meter, 'discover_descendants', return_value=self.response()) as rpc:
            restarted.refresh_descendants()
        self.assertEqual(rpc.call_args.args[1], 'root')
        self.assertEqual(rpc.call_args.kwargs['cursor'], 'page-three')
        self.assertTrue(rpc.call_args.kwargs['archived'])
        self.assertFalse(restarted.descendant_progress['root']['archived'])

    def test_short_visits_resume_rotation_instead_of_rechecking_recent_roots(self):
        roots = {f'root{i}': {'path': f'root{i}.jsonl'} for i in range(5)}
        meter.write_json(self.folder / 'registry.json', roots)
        self.meter.descendant_roots = {key: index for index, key in enumerate(roots)}
        with patch.object(meter, 'discover_descendants', return_value=self.response()) as rpc:
            self.meter.refresh_descendants()
            restarted = meter.Meter(self.folder)
            restarted.refresh_descendants()
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ['root4', 'root3', 'root2', 'root1'])

    def test_missing_live_root_stays_eligible_but_removed_registration_does_not(self):
        with patch.object(meter, 'discover_descendants', return_value=self.response()) as rpc:
            self.meter.refresh_descendants()
            self.meter.descendant_roots = {}
            self.meter.last_descendant_attempt = 0
            self.meter.refresh_descendants()
            self.assertEqual(rpc.call_count, 2)
            meter.write_json(self.folder / 'registry.json', {})
            self.meter.last_descendant_attempt = 0
            self.meter.refresh_descendants()
            self.assertEqual(rpc.call_count, 2)
        checkpoint = meter.read_json(self.folder / 'descendant-sync.json', {})
        self.assertEqual(checkpoint['roots'], {})
        self.assertEqual(checkpoint['progress'], {})

    def test_invalid_or_unregistered_saved_state_cannot_expand_query_scope(self):
        state = {'version': 1, 'roots': {'root': 100, 'unregistered': 1, '../invalid': 2},
                 'progress': {'root': {'cursor': 'saved', 'archived': False, 'checkedAt': float('inf')}}}
        meter.write_json(self.folder / 'descendant-sync.json', state)
        restarted = meter.Meter(self.folder)
        self.assertEqual(restarted.descendant_known_roots, {'root': 100})
        self.assertEqual(restarted.descendant_progress, {})
        for state in ([], {'version': 2}, {'version': 1, 'roots': [], 'progress': {}},
                      {'version': 1, 'roots': {'root': float('nan')}, 'progress': {}}):
            meter.write_json(self.folder / 'descendant-sync.json', state)
            self.assertEqual(meter.Meter(self.folder).descendant_known_roots, {})

    def test_checkpoint_write_failure_keeps_verified_child_and_memory_progress(self):
        original = meter.write_json
        def save(path, value):
            if path.name == 'descendant-sync.json':
                raise OSError('private filesystem detail')
            original(path, value)
        response = self.response(complete=False, nextCursor='page-two',
                                 threads=[{'threadId': 'child', 'path': 'child.jsonl'}])
        with patch.object(meter, 'write_json', side_effect=save), patch.object(meter, 'discover_descendants', return_value=response):
            self.meter.refresh_descendants()
        self.assertIn('child', meter.read_json(self.folder / 'registry.json', {}))
        self.assertEqual(self.meter.descendant_progress['root']['cursor'], 'page-two')
        self.assertFalse(self.meter.descendant_status['progressPersisted'])
        self.assertEqual(self.meter.descendant_status['status'], 'partial')
        self.assertNotIn('private', json.dumps(self.meter.descendant_status))

    def test_verified_root_path_repair_preserves_alias_and_concurrent_hook_path(self):
        for concurrent_path in ('root.jsonl', 'new-hook.jsonl'):
            with self.subTest(concurrent_path=concurrent_path):
                meter.write_json(self.folder / 'registry.json', {'root': {'path': 'root.jsonl'}})
                def reply(*args, **kwargs):
                    meter.write_json(self.folder / 'registry.json', {'root': {'path': concurrent_path, 'alias': 'label'}})
                    return self.response(rootPath='verified-moved-root.jsonl')
                self.meter.last_descendant_attempt = 0
                with patch.object(meter, 'discover_descendants', side_effect=reply):
                    self.meter.refresh_descendants()
                value = meter.read_json(self.folder / 'registry.json', {})['root']
                self.assertEqual(value['alias'], 'label')
                self.assertEqual(value['path'], 'verified-moved-root.jsonl' if concurrent_path == 'root.jsonl' else concurrent_path)

    def test_one_failed_family_does_not_starve_other_roots(self):
        meter.write_json(self.folder / 'registry.json', {'root': {'path': 'root.jsonl'}, 'older': {'path': 'older.jsonl'}})
        self.meter.descendant_roots = {'root': 100, 'older': 99}
        with patch.object(meter, 'discover_descendants', side_effect=[RuntimeError('private'), self.response()]) as rpc:
            self.meter.refresh_descendants()
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ['root', 'older'])
        self.assertEqual(set(self.meter.descendant_progress), {'root', 'older'})
