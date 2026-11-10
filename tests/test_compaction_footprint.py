"""Physical counting keeps data storage out of fixed bookkeeping."""

import json
from pathlib import Path
import tempfile
import unittest

from validate_compaction_scale import footprint
from support import LocalBackendTestCase
import validate_compaction_scale as scale
import test_graphs


class FootprintTests(unittest.TestCase):
    def test_staging_data_is_reported_separately_from_workflow_bookkeeping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('.gwf/gwflow/staging/attempt/value.txt', 'work/value.txt', 'results/value.txt'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('data')
            categories = footprint(root)['categories']
            self.assertEqual(categories['staging_data']['files'], 1)
            self.assertEqual(categories['staging_data']['directories'], 2)
            self.assertEqual(categories['data']['files'], 2)
            self.assertEqual(categories['data']['directories'], 2)
            self.assertEqual(categories['workflow_bookkeeping']['files'], 0)
            self.assertEqual(categories['workflow_bookkeeping']['directories'], 2)


class TerminationFootprintTests(LocalBackendTestCase):
    workers = 4
    task_count = 1
    target_count = 1
    configure_workflow = scale.ScaleCase.configure_workflow
    run_complete = scale.ScaleCase.run_complete
    inject = test_graphs.TaskGraphTests.inject

    def test_target_count_and_submission_history_do_not_grow_bookkeeping(self):
        counts = []
        for targets, retries, recoveries in ((1, 0, 0), (8, 0, 0), (1, 3, 0), (8, 3, 0), (1, 0, 3), (8, 0, 3)):
            with self.subTest(targets=targets, retries=retries, recoveries=recoveries):
                if counts:
                    self.stop_worker(self.worker)
                    self.work = self.make_workdir()
                    self.target_count = targets
                    self.configure_workflow()
                    self.configure()
                    self.worker = self.start_worker()
                for _ in range(recoveries):
                    self.cli('-b', 'recovery_fixture', 'run',
                             env=self.inject(reject_prefix='sample0__gwflow_prepare'), success=False)
                    self.assertIn('Resolved', self.cli('recover', 'sample0'))
                (self.work / 'fail').touch()
                for _ in range(retries):
                    self.cli('run')
                    self.settle()
                (self.work / 'fail').unlink()
                self.run_complete()
                self.assert_bookkeeping(counts)
                ledger = self.work / '.gwf/local-termination/records.json'
                history = json.loads(ledger.read_text())['submissions']
                trace = (self.work / 'trace').read_text()
                self.stop_worker(self.worker)
                self.worker = self.start_worker()
                for cycle in range(2):
                    (self.work / 'results/sample0/value.txt').unlink()
                    self.assertIn('Repair', self.cli('run', '--dry-run'))
                    self.run_complete()
                    self.assert_bookkeeping(counts)
                    after = json.loads(ledger.read_text())['submissions']
                    self.assertEqual({name: after.get(name) for name in history}, history)
                    self.assertEqual((self.work / 'trace').read_text(), trace)
                self.cli('clean', '--delete')
                self.assertIn('Reuse', self.cli('run', '--dry-run'))
                self.assert_bookkeeping(counts)

    def assert_bookkeeping(self, counts):
        measured = footprint(self.work)
        for attempt in measured['attempts'].values():
            self.assertEqual((attempt['files'], attempt['directories']), (2, 1))
        counts.append({category: {kind: value for kind, value in values.items() if kind != 'bytes'}
                       for category, values in measured['categories'].items()
                       if category in ('task_bookkeeping', 'workflow_bookkeeping')})
        self.assertEqual(counts[-1], counts[0])
