"""Physical counting keeps data storage out of fixed bookkeeping."""

from pathlib import Path
import tempfile
import unittest

from validate_compaction_scale import footprint


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
