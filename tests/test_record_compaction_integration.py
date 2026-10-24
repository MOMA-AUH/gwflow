"""Real conversion with installed commands, workers and frontend acknowledgements."""

import json
import shutil
import shlex
from unittest.mock import patch

from gwf.exceptions import WorkflowError
from gwflow import admission
from gwflow.lifecycle import Store

from support import FIXTURES, LocalBackendTestCase
import test_graphs
import test_managed_recovery


class RecordCompactionIntegrationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    launch = test_managed_recovery.ManagedCoordinationTests.launch

    def scheduled_compaction(self, fault=None, **injection):
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        (self.work / 'job-fault.json').write_text(json.dumps(
            dict(compact_completion=True, compaction_fault=fault)))
        env = self.inject(job_fault='sample__gwflow_complete', **injection)
        self.cli('-b', 'recovery_fixture', 'run', env=env)
        return env

    def records(self):
        store = Store(self.work)
        attempt = store.recorded_current('sample')
        intent = admission.read_intent(store, attempt, 'gwflow_complete')
        root = next((self.work / '.gwf/gwflow').glob(f"owners/*/tasks/sample/attempts/{attempt['attempt']}"))
        return store, attempt, intent, root

    def test_real_completion_worker_compacts_before_late_frontend_acknowledgement(self):
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        (self.work / 'job-fault.json').write_text(json.dumps(dict(compact_completion=True)))
        env = self.inject(job_fault='sample__gwflow_complete', hold_after_acceptance='sample__gwflow_complete')
        submitter, output = self.launch('-b', 'recovery_fixture', 'run', env=env)
        self.wait_for(lambda: (self.work / 'acceptance-held').exists())
        try:
            self.finish()
            store, attempt, intent, root = self.records()
            before = json.loads((root / 'record-state.json').read_text())
            ack = f"admissions/{intent['admission']}/ack.json"
            self.assertNotIn(ack, before['records'])
            self.assertEqual(before['phase'], 'compacted')
            lock_inode = (root / 'records.lock').stat().st_ino
        finally:
            (self.work / 'acceptance-release').touch()
        self.assertEqual(submitter.wait(timeout=15), 0, output.read_text())
        self.assertIsNotNone(store.read(attempt, ack, 'submission-ack'))
        after = json.loads((root / 'record-state.json').read_text())
        self.assertEqual({key: after['records'][key] for key in before['records']}, before['records'])
        self.assertEqual((root / 'records.lock').stat().st_ino, lock_inode)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ['record-state.json', 'records.lock'])
        self.assertIn('Reuse', self.cli('run', '--dry-run'))
        self.assertIn(intent['submission'], self.cli('logs', 'sample', '--history', '--details'))
        self.assertIn('reusable', self.cli('status', '--details'))

    def test_real_worker_interruptions_resume_without_recopy_or_recomputation(self):
        # Change the Task identity between journeys so every case uses genuine
        # worker computation and installation before the interrupted conversion.
        for index, fault in enumerate(('before-consolidating', 'after-consolidating',
                                       'during-deletion', 'before-compacted', 'after-compacted')):
            with self.subTest(fault=fault):
                if index:
                    self.configure_workflow(left_command=f'echo journey{index}; printf left > same.txt')
                self.scheduled_compaction(fault)
                self.settle()
                store, attempt, intent, root = self.records()
                completion = store.read(attempt, 'completion.json', 'completion')
                self.assertIsNotNone(completion)
                result = self.work / 'results/sample/result.txt'
                before = result.stat()
                definitions = dict(attempt['executions'])
                store.compact(attempt, intent)
                after = result.stat()
                self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
                self.assertEqual(store.recorded_current('sample')['executions'], definitions)
                self.assertEqual(store.read(attempt, 'completion.json', 'completion'), completion)
                self.assertEqual(json.loads((root / 'record-state.json').read_text())['phase'], 'compacted')
                self.assertEqual(sorted(path.name for path in root.iterdir()), ['record-state.json', 'records.lock'])
                # Scheduler failure remains visible even with checked outputs.
                self.assertIn(intent['submission'], self.cli('logs', 'sample', '--history', '--details'))

    def test_ordinary_run_retries_only_compaction_after_publication_failure(self):
        for index, fault in enumerate(('before-consolidating', 'after-consolidating',
                                       'during-deletion', 'before-compacted', 'after-compacted')):
            with self.subTest(fault=fault):
                self.configure_workflow(left_command=f'echo case{index} >> {shlex.quote(str(self.work / "trace"))}; printf left > same.txt')
                (self.work / 'backend-acceptances.jsonl').unlink(missing_ok=True)
                self.check_ordinary_retry(fault)

    def check_ordinary_retry(self, fault):
        self.scheduled_compaction(fault)
        self.settle()
        store, attempt, intent, root = self.records()
        baseline = store.read(attempt, 'inputs.json', 'inputs')
        completion = store.read(attempt, 'completion.json', 'completion')
        result = self.work / 'results/sample/result.txt'
        before = result.stat()
        trace = (self.work / 'trace').read_text()
        preview = self.cli('run', '--dry-run', '--details')
        self.assertIn('compaction', preview.lower())
        self.assertNotIn('Next action: Reuse', preview)
        self.assertIn('compaction', self.cli('status', '--plain').lower())
        env = self.inject(record_acceptances=True)
        output = self.cli('-b', 'recovery_fixture', 'run', env=env)
        self.assertIn('Submitted 1 job across 1 Task', output)
        self.settle()
        self.assertIn('Reuse', self.cli('run', '--dry-run'))
        current = store.recorded_current('sample')
        self.assertEqual(current, attempt)
        self.assertEqual(store.read(current, 'inputs.json', 'inputs'), baseline)
        self.assertEqual(store.read(current, 'completion.json', 'completion'), completion)
        self.assertEqual((self.work / 'trace').read_text(), trace)
        self.assertEqual((result.stat().st_ino, result.stat().st_mtime_ns),
                         (before.st_ino, before.st_mtime_ns))
        accepted = [json.loads(line) for line in (self.work / 'backend-acceptances.jsonl').read_text().splitlines()]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(accepted[0]['name'].startswith('sample__gwflow_complete__'))
        self.assertNotEqual(accepted[0]['name'], intent['submission'])
        history = self.cli('logs', 'sample', '--history', '--details')
        self.assertIn(intent['submission'], history)
        self.assertIn(accepted[0]['name'], history)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ['record-state.json', 'records.lock'])

    def test_compaction_failure_diagnostic_survives_failure_to_record_it(self):
        self.run_complete()
        store, attempt, intent, root = self.records()
        with patch('os.rmdir', side_effect=OSError('directory removal unavailable')):
            with self.assertRaisesRegex(WorkflowError, 'directory removal'):
                store.compact(attempt, intent)
        outcome_name = f"admissions/{intent['admission']}/outcome.json"
        outcome = store.read(attempt, outcome_name, 'job-outcome')
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(outcome['failure'], 'compaction')
        self.assertIn('directory removal unavailable', outcome['diagnostic'])
        with patch('os.rmdir', side_effect=OSError('original deletion error')), \
                patch.object(store, 'publish', side_effect=OSError('diagnostic disk full')):
            with self.assertRaisesRegex(WorkflowError, 'original deletion error') as raised:
                store.compact(attempt, intent)
        self.assertTrue(any('diagnostic disk full' in note for note in raised.exception.__notes__))
        store.compact(attempt, intent)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ['record-state.json', 'records.lock'])

    def test_changed_admission_operation_attempt_and_retained_outputs_refuse_conversion(self):
        self.run_complete()
        store, attempt, intent, root = self.records()
        for stale in ({**intent, 'admission': '0' * 32}, {**intent, 'submission': 'stale'}):
            with self.subTest(stale=stale):
                with self.assertRaisesRegex(WorkflowError, 'admission changed'):
                    store.compact(attempt, stale)
        stale = {**attempt, 'operation': '0' * 32}
        with self.assertRaisesRegex(WorkflowError, 'checked Completion'):
            store.compact(stale, intent)
        (self.work / 'results/sample/result.txt').write_text('changed')
        with self.assertRaisesRegex(WorkflowError, 'checked Completion'):
            store.compact(attempt, intent)
        self.assertEqual(json.loads((root / 'record-state.json').read_text())['phase'], 'individual')
