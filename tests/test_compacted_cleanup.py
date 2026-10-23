"""Cleanup of genuinely compacted attempts through installed commands."""

import json
from pathlib import Path
from unittest.mock import patch

from gwf.exceptions import WorkflowError
import subprocess
import sys

from gwflow import admission
from gwflow.lifecycle import Store

from support import FIXTURES, LocalBackendTestCase
import test_graphs
import test_cleanup
import test_recovery


class CompactedCleanupTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject

    def compact(self):
        store = Store(self.work)
        attempt = store.recorded_current('sample')
        store.compact(attempt, admission.read_intent(store, attempt, 'gwflow_complete'))
        root = next((self.work / '.gwf/gwflow').glob(
            f"owners/*/tasks/sample/attempts/{attempt['attempt']}"))
        return store, attempt, root

    def fault(self, phase):
        return subprocess.run([
            sys.executable, str(FIXTURES / 'frontend_fault.py'), str(self.work),
            phase, 'sample', 'clean', '--delete', 'sample',
        ], cwd=self.work, capture_output=True, text=True, timeout=30)

    def test_interrupted_intent_publication_leaves_no_metadata_after_retry(self):
        self.run_complete()
        store, attempt, root = self.compact()
        result = self.fault('compacted_cleanup_before_intent')
        self.assertEqual(result.returncode, 114, result.stdout + result.stderr)
        self.assertIsNone(store.cleanup_record(attempt))
        self.assertTrue(store.workspace(attempt).exists())
        self.cli('clean', '--delete', 'sample')
        self.assertFalse(store.workspace(attempt).exists())
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         ['record-state.json', 'records.lock'])

    def test_removal_progress_and_uncertainty_survive_interruption(self):
        self.run_complete()
        store, attempt, root = self.compact()
        workspace = str(store.workspace(attempt))
        interrupted = self.fault('cleanup_after_workspace_remove')
        self.assertEqual(interrupted.returncode, 104, interrupted.stdout + interrupted.stderr)
        record = store.cleanup_record(attempt)
        self.assertEqual(record['removing_path'], workspace)
        self.assertEqual(record['removed_paths'], [])
        # The rmdir happened, but its synchronization and progress publication
        # did not. Retrying must resolve that uncertainty before recording success.
        self.cli('clean', '--delete', 'sample')
        record = store.cleanup_record(attempt)
        self.assertEqual(record['state'], 'removed')
        self.assertIsNone(record['removing_path'])
        self.assertIn(workspace, record['removed_paths'])
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         ['record-state.json', 'records.lock'])


    def test_intent_outcome_and_work_removal_failures_resume_with_bounded_storage(self):
        phases = (
            'compacted_cleanup_after_intent', 'compacted_cleanup_before_outcome',
            'compacted_cleanup_after_outcome', 'compacted_cleanup_error_before_intent',
            'compacted_cleanup_error_after_intent', 'compacted_cleanup_error_before_outcome',
            'compacted_cleanup_error_after_outcome', 'cleanup_during_remove',
        )
        for index, phase in enumerate(phases):
            with self.subTest(phase=phase):
                self.run_complete(*(['--force'] if index else []))
                store, attempt, root = self.compact()
                before = json.loads((root / 'record-state.json').read_text())
                lock = (root / 'records.lock').stat().st_ino
                result = self.fault(phase)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                if phase.endswith('intent'):
                    self.assertTrue(store.workspace(attempt).exists())
                if phase.endswith('outcome'):
                    self.assertFalse(store.workspace(attempt).exists())
                self.cli('clean', '--delete', 'sample')
                self.assertEqual(store.cleanup_record(attempt)['state'], 'removed')
                self.assertFalse(store.workspace(attempt).exists())
                after = json.loads((root / 'record-state.json').read_text())
                self.assertEqual(after['phase'], 'compacted')
                self.assertEqual({key: after['records'][key] for key in before['records']}, before['records'])
                self.assertEqual((root / 'records.lock').stat().st_ino, lock)
                self.assertEqual(sorted(path.name for path in root.iterdir()),
                                 ['record-state.json', 'records.lock'])
                self.assertIn('Reuse', self.cli('run', '--dry-run'))
                self.assertNoSubmissions(self.cli('run'))
        self.assertEqual(len((self.work / 'trace').read_text().splitlines()), 3 * len(phases))

    def test_cleanup_preserves_unfinished_compaction_and_redundant_sources(self):
        self.run_complete()
        store = Store(self.work)
        attempt = store.recorded_current('sample')
        intent = admission.read_intent(store, attempt, 'gwflow_complete')
        with patch('os.rmdir', side_effect=OSError('interrupted conversion')):
            with self.assertRaisesRegex(WorkflowError, 'interrupted conversion'):
                store.compact(attempt, intent)
        root = next((self.work / '.gwf/gwflow').glob(
            f"owners/*/tasks/sample/attempts/{attempt['attempt']}"))
        before = json.loads((root / 'record-state.json').read_text())
        self.assertEqual(before['phase'], 'consolidating')
        originals = {path.relative_to(root): path.read_bytes() for path in root.rglob('*')
                     if path.is_file() and path.name not in ('record-state.json', 'records.lock')}
        self.cli('clean', '--delete', 'sample')
        after = json.loads((root / 'record-state.json').read_text())
        self.assertEqual(after['phase'], 'consolidating')
        self.assertEqual(after['redundant'], before['redundant'])
        self.assertEqual({key: after['records'][key] for key in before['records']}, before['records'])
        self.assertEqual({path: (root / path).read_bytes() for path in originals}, originals)

    def test_activity_results_and_ownership_protections_survive_compaction(self):
        self.run_complete()
        store, attempt, root = self.compact()
        for options, reason, success in (
                ({'job_states': {'sample__left': 'SUBMITTED'}}, 'active work', True),
                ({'conflicting_tracking_prefix': 'sample__left'}, 'unresolved submission', False)):
            output = self.cli('-b', 'recovery_fixture', 'clean', '--delete', 'sample',
                              env=self.inject(**options), success=success)
            self.assertIn(reason, output)
            self.assertTrue(store.workspace(attempt).exists())
        result = self.work / 'results/sample/result.txt'
        result.unlink()
        self.assertIn('keep work for retry or repair', self.cli('clean', '--delete', 'sample'))
        self.assertTrue(store.workspace(attempt).exists())
        # Explicit selection still requires the recorded directory identity.
        workspace = store.workspace(attempt)
        saved = workspace.with_name('saved')
        workspace.rename(saved)
        workspace.mkdir()
        self.assertIn('ownership changed', self.cli('clean', '--delete', '--attempt', attempt['attempt'], success=False))
        self.assertTrue(saved.exists())


    def test_cleanup_keeps_recovery_submission_history_and_log_access(self):
        test_recovery.OperatorRecoveryTests.interrupt_computation(self)
        self.cli('-b', 'recovery_fixture', 'recover', 'sample',
                 env=self.inject(job_states={'sample': 'UNKNOWN'}))
        self.run_complete()
        history = self.cli('logs', 'sample', '--history', '--details')
        logs = self.cli('logs', 'sample__left', '--no-pager')
        store, attempt, root = self.compact()
        records = json.loads((root / 'record-state.json').read_text())['records']
        self.assertTrue(any(name.startswith('recoveries/') for name in records))
        self.cli('clean', '--delete', 'sample')
        self.assertEqual(self.cli('logs', 'sample', '--history', '--details'), history)
        self.assertEqual(self.cli('logs', 'sample__left', '--no-pager'), logs)
        after = json.loads((root / 'record-state.json').read_text())['records']
        self.assertEqual({key: after[key] for key in records}, records)

    def test_default_and_explicit_selection_keep_their_meaning(self):
        self.run_complete()
        store, old, old_root = self.compact()
        self.run_complete('--force')
        store, current, current_root = self.compact()
        self.assertIn('superseded attempt', self.cli('clean', 'sample'))
        self.cli('clean', '--delete', 'sample', 'sam*')
        self.assertTrue(store.workspace(old).exists())
        self.assertFalse(store.workspace(current).exists())
        self.cli('clean', '--delete', '--attempt', old['attempt'])
        self.assertFalse(store.workspace(old).exists())
        for root in (old_root, current_root):
            self.assertEqual(sorted(path.name for path in root.iterdir()),
                             ['record-state.json', 'records.lock'])


class CompactedProducerCleanupTests(LocalBackendTestCase):
    configure_workflow = test_cleanup.CompletedWorkCleanupTests.configure_workflow
    settle = test_cleanup.CompletedWorkCleanupTests.settle
    inject = test_cleanup.CompletedWorkCleanupTests.inject

    def test_historical_transfer_cleanup_gives_up_repair_and_allows_new_consumers(self):
        self.run_complete()
        store = Store(self.work)
        original = store.recorded_current('a')
        result = self.work / 'results/a/result.txt'
        result.unlink()
        self.run_complete('a')
        attempt = store.recorded_current('a')
        self.assertEqual(attempt['attempt'], original['attempt'])
        self.assertGreater(len(store.records(attempt).names('operations')), 1)
        directories = store.staging_directories(attempt)
        self.assertGreater(len(directories), 2)
        store.compact(attempt, admission.read_intent(store, attempt, 'gwflow_complete'))
        root = next((self.work / '.gwf/gwflow').glob(
            f"owners/*/tasks/a/attempts/{attempt['attempt']}"))
        history = self.cli('logs', 'a', '--history', '--details')
        preview = self.cli('clean', 'a')
        for path in directories:
            self.assertIn(path, preview)
        self.cli('clean', '--delete', 'a')
        self.assertEqual(self.cli('logs', 'a', '--history', '--details'), history)
        self.assertFalse(store.workspace(attempt).exists())
        self.assertTrue(all(not Path(path).exists() for path in directories))
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         ['record-state.json', 'records.lock'])
        (self.work / 'workflow.py').write_text(self.with_consumer)
        self.run_complete('c')
        self.assertEqual((self.work / 'trace').read_text().splitlines().count('a'), 1)
        self.assertFalse(store.workspace(attempt).exists())
        self.assertEqual((self.work / 'results/c/result.txt').read_text(), 'a')
        result.unlink()
        self.assertRegex(self.cli('run', 'a', '--dry-run'), r'Task a\s+Run\s+')
