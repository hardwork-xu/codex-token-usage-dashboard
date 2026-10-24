"""Durability tests use only synthetic logs and isolated local directories."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import meter
import usage_log
from test_usage_log import META, THREAD, counters, start, stop, context, snapshot


class ArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'synthetic.jsonl'
        self.rows = [META, start(), context(service_tier='standard'), snapshot(counters()),
                     context(model='gpt-6-sol', service_tier='fast'),
                     snapshot(counters(160,40,20,10), counters()),
                     context(service_tier='standard'),
                     snapshot(counters(240,60,30,15), counters()), stop()]
        self.write()
        meter.write_json(self.folder / 'registry.json', {THREAD: {'path': str(self.path)}})
        self.subject = meter.Meter(self.folder)

    def write(self):
        self.path.write_text(''.join(json.dumps(row)+'\n' for row in self.rows), encoding='utf-8')

    def test_model_speed_sequence_survives_file_loss_restart_and_return(self):
        live = self.subject.snapshot()
        self.assertEqual(live['monitoring']['archive']['savedTasks'], 1)
        turn = live['turns'][0]
        self.assertEqual(turn['tokens']['total'], 300)
        saved = self.subject.archive.load(THREAD)['result']['turns'][0]
        self.assertEqual([(s['model'], s['serviceTier'],s['tokens']['total']) for s in saved['usageSegments']],
                         [('gpt-6-astra','standard',100),('gpt-6-sol','fast',100),('gpt-6-astra','standard',100)])
        self.path.unlink()
        fresh = meter.Meter(self.folder)
        restored = fresh.snapshot()
        self.assertEqual(restored['turns'][0]['tokens'], turn['tokens'])
        self.assertEqual(restored['turns'][0]['pricing']['amount'], turn['pricing']['amount'])
        self.assertEqual(restored['turns'][0]['pricing']['creditEstimate']['credits'], turn['pricing']['creditEstimate']['credits'])
        self.assertEqual(restored['turns'][0]['sourceAvailability'], 'archive')
        self.assertEqual(restored['turns'][0]['quality'], 'partial')
        self.assertEqual(restored['monitoring']['archive']['retainedTasks'], 1)
        self.assertEqual(restored['monitoring']['coverage']['readableTasks'], 0)
        self.assertEqual(restored['monitoring']['coverage']['unreadableTasks'], 1)
        self.write()
        again = fresh.snapshot()
        self.assertEqual(again['turns'][0]['tokens']['total'],300)
        self.assertEqual(again['turns'][0]['sourceAvailability'],'live')
        self.assertEqual(len(fresh.archive.load(THREAD)['result']['turns'][0]['usageSegments']),3)

    def test_truncated_log_does_not_erase_larger_saved_observation(self):
        self.subject.snapshot()
        self.rows = self.rows[:4]
        self.write()
        state = meter.Meter(self.folder).snapshot()
        self.assertEqual(state['turns'][0]['tokens']['total'],300)
        self.assertEqual(state['conversations'][0]['archive']['reason'],'source_changed')
        self.assertEqual(state['turns'][0]['quality'],'partial')
        self.assertEqual(self.subject.archive.load(THREAD)['result']['turns'][0]['tokens']['total'],300)

    def test_poisoned_identity_cannot_revive_after_original_disappears(self):
        self.subject.snapshot()
        self.rows.append({'type':'session_meta','payload':{'id':'foreign-thread'}})
        self.write()
        self.assertEqual(self.subject.snapshot()['turns'],[])
        self.path.unlink()
        self.assertEqual(meter.Meter(self.folder).snapshot()['turns'],[])

    def test_identity_tombstone_blocks_valid_looking_prefix_after_restart(self):
        self.subject.snapshot()
        valid_rows = list(self.rows)
        self.rows.append({'type':'session_meta','payload':{'id':'foreign-thread'}})
        self.write()
        self.assertEqual(self.subject.snapshot()['turns'], [])
        self.assertIsNone(self.subject.archive.load(THREAD))

        fresh = meter.Meter(self.folder)
        # The first bounded pass sees a matching header and all usage records,
        # but has not reached the contradictory final identity again yet.
        with mock.patch.object(usage_log, 'MAX_RECORDS_PER_READ', len(valid_rows)):
            state = fresh.snapshot()
        self.assertFalse(state['conversations'][0]['reading']['complete'])
        self.assertEqual(state['turns'], [])
        self.assertEqual(state['history']['total'], 0)
        self.assertIsNotNone(state['conversations'][0]['readError'])
        self.assertIsNone(fresh.archive.load(THREAD))
        self.assertEqual(fresh.snapshot()['turns'], [])

        # A replacement is admitted only after its full, matching scan.
        self.rows = valid_rows
        self.write()
        repaired = meter.Meter(self.folder).snapshot()
        self.assertEqual(repaired['turns'][0]['tokens']['total'], 300)
        self.assertEqual(repaired['turns'][0]['sourceAvailability'], 'live')

    def test_foreign_replacement_invalidates_saved_usage_across_restart(self):
        self.subject.snapshot()
        self.rows[0] = {'type':'session_meta','payload':{'id':'foreign-thread'}}
        self.write()
        state = meter.Meter(self.folder).snapshot()
        self.assertEqual(state['turns'], [])
        self.assertIsNone(self.subject.archive.load(THREAD))
        self.path.unlink()
        self.assertEqual(meter.Meter(self.folder).snapshot()['turns'], [])

    def test_hook_identity_rejection_invalidates_registered_source_only(self):
        meter.register(self.folder,self.path,THREAD)
        foreign=self.folder/'foreign.jsonl'
        foreign.write_text(json.dumps({'type':'session_meta','payload':{'id':'foreign-thread'}})+'\n')
        with self.assertRaises(usage_log.UsageLogError):
            meter.register(self.folder,foreign,THREAD)
        self.assertIsNotNone(self.subject.archive.load(THREAD))
        self.rows.append({'type':'session_meta','payload':{'id':'foreign-thread'}})
        self.write()
        with self.assertRaises(usage_log.UsageLogError):
            meter.register(self.folder,self.path,THREAD)
        self.assertIsNone(self.subject.archive.load(THREAD))
        self.path.unlink()
        self.assertEqual(meter.Meter(self.folder).snapshot()['turns'],[])

    def test_invalidation_between_retained_save_and_load_hides_prefix(self):
        self.subject.snapshot()
        # A changed source exercises the save/load race. An unchanged source
        # now legitimately returns its identity/hash-verified durable snapshot.
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'response_item','payload':{'synthetic':'ignored'}})+'\n')
        real_save=self.subject.archive.save
        def save_then_invalidate(thread_id,result):
            result=real_save(thread_id,result)
            self.subject.archive.invalidate(thread_id)
            return {**result,'retained':True,'reason':'scan_incomplete'}
        with mock.patch.object(self.subject.archive,'save',side_effect=save_then_invalidate):
            state=self.subject.snapshot()
        self.assertEqual(state['turns'],[])
        self.assertEqual(state['monitoring']['archive']['savedTasks'],0)

    def test_closed_dashboard_hook_preserves_observed_prefix(self):
        meter.register(self.folder,self.path,THREAD)
        self.path.unlink()
        state=meter.Meter(self.folder).snapshot()
        self.assertEqual(state['turns'][0]['tokens']['total'],300)
        self.assertEqual(state['turns'][0]['sourceAvailability'],'archive')

    def test_disk_write_failure_does_not_claim_current_usage_saved(self):
        with mock.patch.object(self.subject.archive,'save',side_effect=OSError('synthetic failure')):
            state=self.subject.snapshot()
        self.assertEqual(state['turns'][0]['tokens']['total'],300)
        self.assertEqual(state['monitoring']['archive']['status'],'error')
        self.assertEqual(state['monitoring']['archive']['savedTasks'],0)
        self.assertEqual(state['monitoring']['archive']['writeErrors'],1)

    def test_unknown_speed_stays_unknown_and_windows_reproject_after_log_loss(self):
        self.rows=[META,start(),context(),snapshot(counters()),stop()]
        self.write()
        self.subject.snapshot()
        self.path.unlink()
        saved=self.subject.archive.load(THREAD)['result']
        timestamp=saved['turns'][0]['usageSegments'][0]['timestamp']
        windows={'2026-09-14':{'startAt':int(timestamp),'endAt':int(timestamp)+86400,'timeZone':'UTC'}}
        projected=usage_log.reproject_usage(saved,windows)
        self.assertIsNone(projected['turns'][0]['usageSlices'][0]['serviceTier'])
        self.assertEqual(projected['turns'][0]['usageSlices'][0]['speedOverrideDay'],'2026-09-14')
        self.assertEqual(projected['turns'][0]['tokens'],saved['turns'][0]['tokens'])
        self.assertIsNone(saved['turns'][0]['usageSlices'][0]['speedOverrideDay'])

    def test_archived_running_question_is_not_presented_as_currently_active(self):
        self.rows=self.rows[:-1]
        self.write()
        self.subject.snapshot()
        self.path.unlink()
        state=meter.Meter(self.folder).snapshot()
        self.assertEqual(state['turns'][0]['status'],'unknown')
        self.assertEqual(state['conversations'][0]['activeTurnCount'],0)


if __name__ == '__main__':
    unittest.main()
