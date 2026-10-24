"""Unfinished compaction through installed commands and actual worker processes."""

import json
import shlex
import subprocess
import sys

from gwflow import admission
from gwflow.lifecycle import Store

from support import LocalBackendTestCase
import test_graphs
import test_interactive_status
import test_record_compaction_integration


class CompactionRetryTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    scheduled_compaction = test_record_compaction_integration.RecordCompactionIntegrationTests.scheduled_compaction
    records = test_record_compaction_integration.RecordCompactionIntegrationTests.records
    terminal = test_interactive_status.InteractiveStatusTests.terminal

    def failed_compaction(self, fault='after-consolidating'):
        self.scheduled_compaction(fault)
        self.settle()
        return self.records()

    def add_consumer(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace("gwf.task(task, alias='sample')", "producer = gwf.task(task, alias='sample')") +
            "consumer = empty_task(inputs=[producer.outputs['joined']])\n"
            "target = consumer.target('compute', inputs=consumer.inputs, outputs=['value.txt'])\n"
            f"target << shell({'echo consumer >> ' + shlex.quote(str(self.work / 'trace')) + '; cat {source} > {out}'!r}, source=consumer.inputs[0], out=target.output('value.txt'))\n"
            "consumer.retain('value', source=target.output('value.txt'), path='value.txt')\n"
            "gwf.task(consumer, alias='consumer')\n")

    def test_interactive_status_explains_compaction_separately_from_computation(self):
        self.failed_compaction()
        session = self.terminal('status', 'sample', '--details')
        session.wait_for('compaction unfinished')
        session.wait_for('4/5')
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        details = self.cli('status', 'sample', '--plain', '--details')
        self.assertIn('record compaction unfinished', details)
        self.assertIn('gwflow run', details)

    def test_scheduler_uncertainty_recovery_and_historical_activity_preserve_obligation(self):
        store, attempt, intent, root = self.failed_compaction()
        for state in ('SUBMITTED', 'RUNNING', 'UNKNOWN'):
            env = self.inject(job_states={'sample__gwflow_complete': state})
            output = self.cli('-b', 'recovery_fixture', 'run', '--dry-run', env=env,
                              success=state != 'UNKNOWN')
            self.assertNoSubmissions(output)
            self.assertNotIn('Reuse', output)
            self.assertIn('Blocked' if state == 'UNKNOWN' else 'Wait', output)
        env = self.inject(job_states={'sample__gwflow_complete': 'UNKNOWN'})
        self.assertIn('Resolved submission', self.cli('-b', 'recovery_fixture', 'recover', 'sample', env=env))
        self.assertFalse(store.compaction_succeeded(attempt))
        self.assertIn('compaction', self.cli('-b', 'recovery_fixture', 'run', '--dry-run', env=env))
        self.cli('run')
        self.settle()
        self.assertIn('Reuse', self.cli('run', '--dry-run'))
        env = self.inject(submission_states={intent['submission']: 'RUNNING'})
        for arguments in (('run',), ('run', '--force'), ('clean', '--delete', 'sample')):
            output = self.cli('-b', 'recovery_fixture', *arguments, env=env, success=arguments[0] == 'clean')
            self.assertIn(intent['submission'], output)
        self.assertTrue(store.workspace(attempt).exists())

    def test_cancellation_and_known_failure_override_published_compaction_success(self):
        self.scheduled_compaction()
        self.settle()
        store, attempt, intent, root = self.records()
        self.assertTrue(store.compaction_succeeded(attempt))
        for state, label in (('CANCELLED', 'canceled'), ('FAILED', 'failed')):
            env = self.inject(job_states={'sample__gwflow_complete': state})
            status = self.cli('-b', 'recovery_fixture', 'status', 'sample', '--plain', env=env)
            self.assertRegex(status, rf'(?m)^sample\s+{label}\s+4/5')
            self.assertIn('compaction', status)
            self.assertNotIn('Reuse', self.cli('-b', 'recovery_fixture', 'run', '--dry-run', env=env))
        env = self.inject(job_states={'sample': 'UNKNOWN'})
        self.assertIn('Reuse', self.cli('-b', 'recovery_fixture', 'run', '--dry-run', env=env))
        self.assertIn(intent['submission'], self.cli('logs', 'sample', '--history', '--details'))

    def test_cleanup_keeps_unfinished_obligation_and_retry_needs_no_computation_files(self):
        store, attempt, intent, root = self.failed_compaction()
        result = self.work / 'results/sample/result.txt'
        before = result.stat()
        self.cli('clean', '--delete', '--attempt', attempt['attempt'])
        self.assertFalse(store.workspace(attempt).exists())
        self.assertFalse(store.compaction_succeeded(attempt))
        self.assertIn('compaction', self.cli('run', '--dry-run'))
        self.cli('run')
        self.settle()
        self.assertIn('Reuse', self.cli('run', '--dry-run'))
        self.assertEqual((result.stat().st_ino, result.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertFalse(store.workspace(attempt).exists())
        self.assertEqual(sorted(path.name for path in root.iterdir()), ['record-state.json', 'records.lock'])

    def test_changed_retained_outputs_select_repair_and_new_operation(self):
        store, attempt, intent, root = self.failed_compaction()
        result = self.work / 'results/sample/result.txt'
        result.write_text('damaged')
        self.assertRegex(self.cli('run', '--dry-run'), r'Task sample\s+Repair\s+')
        self.cli('run')
        self.settle()
        current = store.recorded_current('sample')
        self.assertEqual(current['attempt'], attempt['attempt'])
        self.assertEqual(current['executions'], attempt['executions'])
        self.assertNotEqual(current['operation'], attempt['operation'])
        self.assertTrue(store.compaction_succeeded(current))
        self.assertEqual(result.read_text(), 'leftright')
        self.assertEqual(len((self.work / 'trace').read_text().splitlines()), 3)

    def test_changed_definition_and_input_baseline_select_fresh_work(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('empty_task(inputs=[])', "empty_task(inputs=['input.txt'])"))
        store, attempt, intent, root = self.failed_compaction()
        (self.work / 'input.txt').write_text('changed accepted input')
        self.assertIn('input metadata changed', self.cli('run', '--dry-run'))
        self.cli('run')
        self.settle()
        self.assertNotEqual(store.recorded_current('sample')['attempt'], attempt['attempt'])
        workflow.write_text(workflow.read_text().replace('printf left', 'printf edited'))
        self.assertIn('changed commands', self.cli('run', '--dry-run'))

    def test_dependents_are_resubmitted_after_completion_only_retry(self):
        self.add_consumer()
        store, attempt, intent, root = self.failed_compaction()
        self.assertNotIn('consumer', (self.work / 'trace').read_text().splitlines())
        consumer = store.recorded_current('consumer')
        old = admission.read_intent(store, consumer, 'gwflow_prepare')
        preview = self.cli('run', '--dry-run')
        self.assertNotIn('Reuse', preview)
        self.cli('-b', 'recovery_fixture', 'run', env=self.inject(record_acceptances=True))
        self.settle()
        self.assertEqual((self.work / 'results/consumer/value.txt').read_text(), 'leftright')
        self.assertEqual(len((self.work / 'trace').read_text().splitlines()), 4)
        self.assertEqual(store.recorded_current('sample'), attempt)
        self.assertEqual(store.recorded_current('consumer')['attempt'], consumer['attempt'])
        new = admission.read_intent(store, store.recorded_current('consumer'), 'gwflow_prepare')
        self.assertNotEqual(old['submission'], new['submission'])
        accepted = [json.loads(line)['name'] for line in (self.work / 'backend-acceptances.jsonl').read_text().splitlines()]
        self.assertEqual(len(accepted), 4)
        self.assertEqual(sum(name.startswith('sample__gwflow_complete__') for name in accepted), 1)
        self.assertEqual(self.cli('run', '--dry-run').count('Reuse'), 2)

    def test_older_success_cannot_satisfy_a_new_failed_submission_or_repair_operation(self):
        self.scheduled_compaction()
        self.settle()
        store, attempt, successful, root = self.records()
        # A known failed scheduler result requires another completion submission,
        # even though the previous worker managed to publish its final document.
        (self.work / 'job-fault.json').write_text(json.dumps(dict(
            compaction_fault='before-compacted')))
        env = self.inject(job_fault='sample__gwflow_complete',
                          submission_states={successful['submission']: 'FAILED'})
        self.cli('-b', 'recovery_fixture', 'run', env=env)
        self.settle()
        failed = admission.read_intent(store, attempt, 'gwflow_complete')
        self.assertNotEqual(failed['submission'], successful['submission'])
        self.assertFalse(store.compaction_succeeded(attempt))
        self.assertIn('compaction', self.cli('run', '--dry-run'))
        # Repair selects a new operation; old complete outcomes cannot satisfy it.
        (self.work / 'results/sample/result.txt').unlink()
        self.cli('-b', 'recovery_fixture', 'run', env=self.inject(job_fault='sample__gwflow_complete'))
        self.settle()
        repaired = store.recorded_current('sample')
        self.assertNotEqual(repaired['operation'], attempt['operation'])
        self.assertFalse(store.compaction_succeeded(repaired))
        self.assertIn('compaction', self.cli('run', '--dry-run'))
        self.cli('run')
        self.settle()
        self.assertTrue(store.compaction_succeeded(repaired))
        self.assertEqual(len((self.work / 'trace').read_text().splitlines()), 3)

    def test_worker_refuses_producer_with_unfinished_compaction_despite_valid_results(self):
        self.add_consumer()
        store, attempt, intent, root = self.failed_compaction()
        consumer = store.recorded_current('consumer')
        prepare = admission.read_intent(store, consumer, 'gwflow_prepare')
        # Execute the admitted worker itself to model a backend that releases a
        # dependent job despite the producer's nonzero exit status.
        result = subprocess.run([sys.executable, '-m', 'gwflow.execution', str(store.owner_path),
                                 'consumer', consumer['attempt'], 'prepare', prepare['admission']],
                                cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('required record compaction', result.stderr)
        self.assertNotIn('consumer', (self.work / 'trace').read_text().splitlines())
        self.assertFalse((self.work / 'results/consumer').exists())

    def test_real_io_failure_keeps_compaction_diagnostic_after_scheduler_history_expires(self):
        for index, fault in enumerate(('error-during-deletion', 'error-before-compacted')):
            with self.subTest(fault=fault):
                self.configure_workflow(left_command=f'printf left > same.txt; echo {index} >> {shlex.quote(str(self.work / "trace"))}')
                store, attempt, intent, root = self.failed_compaction(fault)
                outcome = store.read(attempt, f"admissions/{intent['admission']}/outcome.json", 'job-outcome')
                self.assertEqual(outcome['failure'], 'compaction')
                self.assertEqual(outcome['state'], 'failed')
                self.assertIn('injected compaction', outcome['diagnostic'])
                self.assertIn('injected compaction', self.cli('logs', intent['submission'], '--stderr', '--no-pager'))
                env = self.inject(job_states={'sample': 'UNKNOWN'})
                self.assertIn('compaction', self.cli('-b', 'recovery_fixture', 'run', '--dry-run', env=env))
                self.cli('run')
                self.settle()
                self.assertIn('Reuse', self.cli('run', '--dry-run'))

    def test_successful_historical_completion_survives_repair_and_scheduler_expiry(self):
        self.scheduled_compaction()
        self.settle()
        store, attempt, successful, root = self.records()
        (self.work / 'results/sample/result.txt').unlink()
        self.cli('run')
        self.settle()
        expected = f"sample__gwflow_complete: complete (historical); submission {successful['submission']}"
        self.assertIn(expected, self.cli('logs', 'sample', '--history', '--details'))
        env = self.inject(job_states={'sample': 'UNKNOWN'})
        self.assertIn(expected, self.cli('-b', 'recovery_fixture', 'logs', 'sample', '--history', '--details', env=env))

    def test_canceled_compaction_retry_remains_retryable_without_recomputing(self):
        store, attempt, intent, root = self.failed_compaction()
        (self.work / 'job-fault.json').write_text(json.dumps(dict(gate_before_preparation=True)))
        self.cli('-b', 'recovery_fixture', 'run', env=self.inject(job_fault='sample__gwflow_complete'))
        self.wait_for(lambda: (self.work / 'preparation-held').exists())
        try:
            self.assertIn('Wait', self.cli('run', '--dry-run'))
            self.cli('cancel', 'sample')
            self.settle()
            self.assertIn('canceled', self.cli('status', 'sample', '--plain'))
            self.assertIn('compaction', self.cli('run', '--dry-run'))
        finally:
            (self.work / 'preparation-release').touch()
        self.cli('run')
        self.settle()
        self.assertEqual(store.recorded_current('sample'), attempt)
        self.assertTrue(store.compaction_succeeded(attempt))
        self.assertEqual(len((self.work / 'trace').read_text().splitlines()), 3)
