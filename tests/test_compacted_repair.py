"""Repair compacted attempts with installed commands and real scheduled workers."""

import json

from gwflow.lifecycle import Store

from support import LocalBackendTestCase
import test_repair
import test_transfer


class CompactedRepairTests(LocalBackendTestCase):
    configure_workflow = test_transfer.TransferRecoveryTests.configure_workflow
    assert_results = test_transfer.TransferRecoveryTests.assert_results
    settle = test_transfer.TransferRecoveryTests.settle
    inject = test_transfer.TransferRecoveryTests.inject
    fault = test_transfer.TransferRecoveryTests.fault
    add_consumer = test_transfer.TransferRecoveryTests.add_consumer
    separate_filesystem = test_transfer.TransferRecoveryTests.separate_filesystem

    def complete_compacted(self, **faults):
        self.cli('-b', 'recovery_fixture', 'run', env=self.fault(compact_completion=True, **faults))
        self.settle()
        store = Store(self.work)
        attempt = store.recorded_current('a')
        root = next((self.work / '.gwf/gwflow').glob(f"owners/*/tasks/a/attempts/{attempt['attempt']}"))
        return store, attempt, root

    def assert_compacted(self, root):
        self.assertEqual(json.loads((root / 'record-state.json').read_text())['phase'], 'compacted')
        self.assertEqual(sorted(path.name for path in root.iterdir()), ['record-state.json', 'records.lock'])

    def test_repeated_repair_restores_before_submission_and_recompacts_history(self):
        store, before, root = self.complete_compacted()
        self.assert_compacted(root)
        lock = (root / 'records.lock').stat().st_ino
        for damage in ('missing', 'changed'):
            with self.subTest(damage=damage):
                history = json.loads((root / 'record-state.json').read_text())['records']
                output = self.work / 'results/samples/a/report/renamed.txt'
                if damage == 'missing':
                    output.unlink()
                else:
                    output.write_text('damaged')
                snapshot = (root / 'record-state.json').read_bytes()
                self.assertRegex(self.cli('run', '--dry-run'), r'Task a\s+Repair\s+')
                self.assertEqual((root / 'record-state.json').read_bytes(), snapshot)
                self.cli('-b', 'recovery_fixture', 'run',
                         env=self.inject(reject_before_admission=True), success=False)
                self.assertEqual(json.loads((root / 'record-state.json').read_text())['phase'], 'individual')
                current = store.recorded_current('a')
                self.assertEqual(current['attempt'], before['attempt'])
                self.assertEqual(current['executions'], before['executions'])
                self.assertNotEqual(current['operation'], before['operation'])
                self.assertFalse(store.completed(current))
                self.complete_compacted()
                self.assert_compacted(root)
                self.assert_results(output.parent)
                after = json.loads((root / 'record-state.json').read_text())['records']
                for name, value in history.items():
                    if name != 'attempt.json':
                        self.assertEqual(after[name], value)
                self.assertIn('reusable', self.cli('status', '--details'))
                self.assertIn('Reuse', self.cli('run', '--dry-run'))
                self.assertIn(before['jobs']['gwflow_complete'], self.cli('logs', 'a', '--history', '--details'))
        self.assertEqual((self.work / 'trace').read_text().splitlines(), ['compute'])
        self.assertEqual((root / 'records.lock').stat().st_ino, lock)

    def test_frontend_interruptions_never_admit_workers_before_durable_restoration(self):
        import subprocess
        import sys
        from support import FIXTURES

        store, before, root = self.complete_compacted()
        phases = ('restoration_before_restoring', 'restoration_after_restoring',
                  'restoration_during_reconstruction', 'restoration_during_install',
                  'restoration_before_individual', 'restoration_after_individual',
                  'restoration_error_after_individual', 'before_repair_intent',
                  'before_repair_selection', 'after_repair_selection')
        for phase in phases:
            with self.subTest(phase=phase):
                result = self.work / 'results/samples/a/report/renamed.txt'
                result.write_text('damaged before restoration')
                env = self.inject(job_fault='a__gwflow_complete', record_acceptances=True)
                acceptances = self.work / 'backend-acceptances.jsonl'
                snapshot = acceptances.read_bytes() if acceptances.exists() else b''
                outcome = subprocess.run([
                    sys.executable, str(FIXTURES / 'frontend_fault.py'), str(self.work), phase,
                    'a', '-b', 'recovery_fixture', 'run',
                ], cwd=self.work, capture_output=True, text=True, env=env, timeout=30)
                expected = {'restoration_during_reconstruction': 119, 'restoration_during_install': 118,
                            'restoration_error_after_individual': 1, 'before_repair_intent': 99,
                            'before_repair_selection': 100, 'after_repair_selection': 101}
                code = expected.get(phase, 116 if '_before_' in phase else 117)
                self.assertEqual(outcome.returncode, code, outcome.stdout + outcome.stderr)
                self.assertEqual(acceptances.read_bytes() if acceptances.exists() else b'', snapshot)
                self.assertEqual(result.read_text(), 'damaged before restoration')
                self.assertEqual(store.recorded_current('a')['attempt'], before['attempt'])
                self.assertRegex(self.cli('run', '--dry-run'), r'Task a\s+(Repair|Finish)\s+')
                self.complete_compacted()
                self.assert_compacted(root)
                self.assert_results(result.parent)
                self.assertEqual(store.recorded_current('a')['executions'], before['executions'])
        self.assertEqual((self.work / 'trace').read_text().splitlines(), ['compute'])

    def test_installed_repair_recompacts_after_source_loss(self):
        import shutil

        store, before, root = self.complete_compacted()
        (self.work / 'results/samples/a/report/renamed.txt').unlink()
        self.complete_compacted(crash_after_results_install=True)
        self.assertEqual(json.loads((root / 'record-state.json').read_text())['phase'], 'individual')
        selected = store.recorded_current('a')
        shutil.rmtree(self.work / 'work')
        self.assertIn('finish checked installed', self.cli('run', '--dry-run'))
        self.complete_compacted()
        self.assert_compacted(root)
        self.assertEqual(store.recorded_current('a'), selected)
        self.assertEqual(selected['attempt'], before['attempt'])
        self.assertFalse((self.work / 'work').exists())
        self.assertEqual((self.work / 'trace').read_text().splitlines(), ['compute'])

    def test_cleanup_intent_and_missing_work_prevent_repair(self):
        import subprocess
        import sys
        from support import FIXTURES

        store, before, root = self.complete_compacted()
        outcome = subprocess.run([
            sys.executable, str(FIXTURES / 'frontend_fault.py'), str(self.work),
            'compacted_cleanup_after_intent', 'a', 'clean', '--delete', 'a',
        ], cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(outcome.returncode, 115, outcome.stdout + outcome.stderr)
        (self.work / 'results/samples/a/report/renamed.txt').unlink()
        self.assertNotIn('Repair', self.cli('run', '--dry-run'))
        self.cli('clean', '--delete', 'a')
        self.assertNotIn('Repair', self.cli('run', '--dry-run'))
        self.complete_compacted()
        self.assertNotEqual(store.recorded_current('a')['attempt'], before['attempt'])
        self.assertEqual((self.work / 'trace').read_text().splitlines(), ['compute', 'compute'])

    # Reuse existing installed-command protection journeys with genuinely
    # compacted initial attempts and completion workers on every normal run.
    def run_complete(self, *args):
        self.cli('-b', 'recovery_fixture', 'run', *args, env=self.fault(compact_completion=True))
        self.settle()
        attempt = Store.for_job(self.work / '.gwf/gwflow/owner.json').recorded_current('a')
        root = next((self.work / '.gwf/gwflow').glob(f"owners/*/tasks/a/attempts/{attempt['attempt']}"))
        self.assert_compacted(root)

    attempt = test_repair.ResultsRepairTests.attempt
    test_command_tracking_governs_repair_eligibility = test_repair.ResultsRepairTests.test_command_tracking_governs_repair_eligibility
    test_queued_consumer_blocks_repair_after_its_declaration_is_removed = test_repair.ResultsRepairTests.test_queued_consumer_blocks_repair_after_its_declaration_is_removed
    test_uncertain_consumer_blocks_repair = test_repair.ResultsRepairTests.test_uncertain_consumer_blocks_repair
    test_repair_on_separate_filesystem_recreates_missing_staging_root = test_repair.ResultsRepairTests.test_repair_on_separate_filesystem_recreates_missing_staging_root

    def test_changed_inputs_require_fresh_computation_after_compaction(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('empty_task(inputs=[])', "empty_task(inputs=['input.txt'])"))
        store, before, root = self.complete_compacted()
        (self.work / 'results/samples/a/report/renamed.txt').unlink()
        (self.work / 'input.txt').write_text('changed external input')
        self.assertRegex(self.cli('run', '--dry-run'), r'Task a\s+Run\s+')
        self.complete_compacted()
        self.assertNotEqual(store.recorded_current('a')['attempt'], before['attempt'])
        self.assertEqual((self.work / 'trace').read_text().splitlines(), ['compute', 'compute'])
        self.assert_compacted(root)
