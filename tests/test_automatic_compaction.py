"""Automatic completion contracts through installed commands and real workers."""

import json
import shutil
import subprocess

from gwflow import admission
from gwflow.lifecycle import Store

from support import FIXTURES, LocalBackendTestCase
import test_compaction_retry
import test_graphs
import test_managed_recovery


class AutomaticCompactionTests(LocalBackendTestCase):
    workers = 4
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    add_consumer = test_compaction_retry.CompactionRetryTests.add_consumer
    launch = test_managed_recovery.ManagedCoordinationTests.launch
    slurm_environment = test_managed_recovery.ManagedCoordinationTests.slurm_environment

    def assert_footprint(self, store, attempt):
        root = next((self.work / '.gwf/gwflow').glob(
            f"owners/*/tasks/{attempt['task']}/attempts/{attempt['attempt']}"))
        self.assertTrue(store.compaction_succeeded(attempt))
        self.assertEqual(sorted(path.name for path in root.iterdir()), ['record-state.json', 'records.lock'])
        return root

    def test_operation_requires_compaction_before_any_worker_finishes(self):
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        (self.work / 'job-fault.json').write_text(json.dumps(dict(gate_before_preparation=True)))
        self.cli('-b', 'recovery_fixture', 'run', env=self.inject(job_fault='sample__gwflow_prepare'))
        self.wait_for(lambda: (self.work / 'preparation-held').exists())
        try:
            store = Store(self.work)
            attempt = store.recorded_current('sample')
            obligation = store.read(attempt, 'compaction.json', 'compaction')
            self.assertEqual(obligation['operation'], attempt['operation'])
            self.assertFalse(store.compaction_succeeded(attempt))
        finally:
            (self.work / 'preparation-release').touch()
        self.finish()
        root = self.assert_footprint(store, attempt)
        state = json.loads((root / 'record-state.json').read_text())
        del state['records'][f"operations/{attempt['operation']}/compaction.json"]
        (root / 'record-state.json').write_text(json.dumps(state))
        self.assertIn('compaction obligation', self.cli('run', '--dry-run', success=False))

    def test_lost_completion_acknowledgement_reconciles_in_compacted_storage(self):
        self.cli('-b', 'recovery_fixture', 'run', env=self.inject(lose_ack='sample__gwflow_complete'), success=False)
        self.finish()
        store = Store(self.work)
        attempt = store.recorded_current('sample')
        intent = admission.read_intent(store, attempt, 'gwflow_complete')
        ack = f"admissions/{intent['admission']}/ack.json"
        self.assertIsNone(store.read(attempt, ack, 'submission-ack'))
        root = self.assert_footprint(store, attempt)
        before = json.loads((root / 'record-state.json').read_text())['records']
        self.assertNoSubmissions(self.cli('run'))
        self.assertIsNotNone(store.read(attempt, ack, 'submission-ack'))
        after = json.loads((root / 'record-state.json').read_text())['records']
        self.assertEqual({name: after[name] for name in before}, before)
        self.assert_footprint(store, attempt)

    def test_simultaneous_completions_and_producer_reads_keep_independent_records(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() + "gwf.task(task, alias='peer')\n")
        self.add_consumer()
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        (self.work / 'job-fault.json').write_text(json.dumps(dict(gate_before_compaction=True)))
        self.cli('-b', 'recovery_fixture', 'run', env=self.inject(
            job_fault_prefixes=['sample__gwflow_complete', 'peer__gwflow_complete']))
        for name in ('sample', 'peer'):
            self.wait_for(lambda name=name: (self.work / (name + '-compaction-held')).exists())
        store = Store(self.work)
        try:
            for name in ('sample', 'peer'):
                attempt = store.recorded_current(name)
                self.assertTrue(store.completed(attempt))
                self.assertFalse(store.compaction_succeeded(attempt))
            inspector, output = self.launch('status', '--plain', '--details')
        finally:
            for name in ('sample', 'peer'):
                (self.work / (name + '-compaction-release')).touch()
        self.assertEqual(inspector.wait(timeout=20), 0, output.read_text())
        self.finish()
        self.assertEqual((self.work / 'results/consumer/value.txt').read_text(), 'leftright')
        for name in ('sample', 'peer', 'consumer'):
            self.assert_footprint(store, store.recorded_current(name))
        self.assertNoSubmissions(self.cli('run'))

    def test_pinned_slurm_blocks_dependents_after_compaction_failure_and_retries(self):
        self.add_consumer()
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        (self.work / 'job-fault.json').write_text(json.dumps(dict(compaction_fault='error-during-deletion')))
        env = self.slurm_environment()
        self.cli('-b', 'slurm', 'run', env={**env, 'GWFLOW_TEST_SLURM_JOB_FAULT': 'sample__gwflow_complete'})
        self.settle()
        submitted = [json.loads(line) for line in (self.work / 'slurm-submitted.jsonl').read_text().splitlines()]
        completion = next(item for item in submitted if item['name'].startswith('sample__gwflow_complete__'))
        consumer = next(item for item in submitted if item['name'].startswith('consumer__gwflow_prepare__'))
        self.assertEqual(consumer['args'], ['--parsable', '--dependency=afterok:' + completion['id']])
        states = subprocess.run([str(self.work / 'slurm-bin/sacct')], env=env,
                                capture_output=True, text=True, check=True).stdout
        self.assertIn(completion['id'] + '|FAILED', states)
        self.assertNotIn('consumer', (self.work / 'trace').read_text().splitlines())
        store = Store(self.work)
        attempt = store.recorded_current('sample')
        self.assertTrue(store.completed(attempt))
        self.assertFalse(store.compaction_succeeded(attempt))
        self.assertIn('compaction', self.cli('-b', 'slurm', 'run', '--dry-run', env=env))
        self.cli('-b', 'slurm', 'run', env=env)
        self.settle()
        self.assertEqual(store.recorded_current('sample'), attempt)
        self.assertEqual(len((self.work / 'trace').read_text().splitlines()), 4)
        self.assert_footprint(store, attempt)
        self.assert_footprint(store, store.recorded_current('consumer'))
        (self.work / 'forgotten-slurm-history').touch()
        self.assertNoSubmissions(self.cli('-b', 'slurm', 'run', env=env))
        self.assertIn(completion['name'], self.cli('-b', 'slurm', 'logs', 'sample', '--history', '--details', env=env))
