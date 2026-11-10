"""Late and interrupted evidence publication through installed local workers."""

import json
import os
import shutil

from support import FIXTURES, LocalBackendTestCase
import test_graphs


class TerminationPublicationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow

    def hold_completion_receipt(self):
        self.stop_worker(self.worker)
        shutil.copy(FIXTURES / 'termination_publication.py', self.work / 'sitecustomize.py')
        self.worker = self.start_worker(env={**os.environ, 'PYTHONPATH': str(self.work),
                                            'GWFLOW_TERMINATION_GATE': str(self.work)})
        self.addCleanup((self.work / 'publication-release').touch)
        self.cli('run')
        self.wait_for((self.work / 'publication-held').exists)
        state = next((self.work / '.gwf/gwflow').rglob('record-state.json'))
        self.assertEqual(json.loads(state.read_text())['phase'], 'compacted')
        self.assertEqual(sorted(path.name for path in state.parent.iterdir()),
                         ['record-state.json', 'records.lock'])
        self.assertEqual((self.work / 'results/sample/result.txt').read_text(), 'leftright')
        return (self.work / 'trace').read_text()

    def assert_bounded_and_reusable(self, trace):
        self.assertIn('Reuse', self.cli('run', '--dry-run'))
        self.cli('clean', '--delete')
        self.assertIn('Reuse', self.cli('run', '--dry-run'))
        self.assertEqual((self.work / 'trace').read_text(), trace)
        self.assertEqual(sorted(path.name for path in (self.work / '.gwf/local-termination').iterdir()),
                         ['records.json', 'records.lock'])

    def test_completion_publishes_after_compaction_without_restoring_loose_files(self):
        trace = self.hold_completion_receipt()
        # An independent backend observes the ledger while its writer is held.
        self.assertIn('cannot confirm computation descendants have stopped', self.cli(
            '-b', 'state_fixture', 'run', '--dry-run', env=self.state_backend({}), success=False))
        (self.work / 'publication-release').touch()
        self.finish()
        self.stop_worker(self.worker)
        self.worker = self.start_worker()
        self.assert_bounded_and_reusable(trace)

    def test_interrupted_completion_publication_recovers_without_deleting_evidence(self):
        trace = self.hold_completion_receipt()
        self.worker.kill()
        self.worker.communicate(timeout=10)
        ledger = self.work / '.gwf/local-termination'
        self.assertTrue(list(ledger.glob('.pending-*')))
        before = json.loads((ledger / 'records.json').read_text())
        self.worker = self.start_worker()
        for arguments in (('run', '--dry-run'), ('run', '--force'), ('clean', '--delete')):
            self.assertIn('cannot confirm computation descendants have stopped', self.cli(*arguments, success=False))
        self.cli('recover', 'sample')
        self.assertNoSubmissions(self.cli('run'))
        self.assertEqual(json.loads((ledger / 'records.json').read_text()), before)
        self.assert_bounded_and_reusable(trace)
