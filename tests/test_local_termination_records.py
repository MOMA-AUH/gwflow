"""Independent publishers share the durable Submission evidence boundary."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gwf.backends.local import LocalStatus
from gwflow._local_termination import TerminationRecords


class TerminationRecordTests(unittest.TestCase):
    def test_concurrent_publishers_preserve_every_exact_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = """
from pathlib import Path
import sys
import time
from gwf.backends.local import LocalStatus
from gwflow._local_termination import TerminationRecords
root, publisher = sys.argv[1:]
records = TerminationRecords(root)
deadline = time.monotonic() + 10
while not (Path(root) / 'release').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('Publisher gate timed out')
    time.sleep(.01)
for number in range(16):
    name = f'sample__compute__{publisher}_{number}'
    records.record(name, LocalStatus.UNKNOWN)
    records.record(name, LocalStatus.COMPLETED)
"""
            publishers = [subprocess.Popen([sys.executable, '-c', code, temporary, str(number)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                          for number in range(4)]
            try:
                (Path(temporary) / 'release').touch()
                for publisher in publishers:
                    stdout, stderr = publisher.communicate(timeout=20)
                    self.assertEqual(publisher.returncode, 0, stdout + stderr)
            finally:
                for publisher in publishers:
                    if publisher.poll() is None:
                        publisher.kill()
                        publisher.communicate()
            records = TerminationRecords(temporary)
            expected = {f'sample__compute__{publisher}_{number}': 'COMPLETED'
                        for publisher in range(4) for number in range(16)}
            ledger = Path(temporary) / '.gwf/local-termination'
            self.assertEqual(json.loads((ledger / 'records.json').read_text())['submissions'], expected)
            for submission in expected:
                self.assertEqual(records.read(submission), LocalStatus.COMPLETED)
                self.assertEqual(records.read(submission + '_other'), LocalStatus.UNKNOWN)
            self.assertEqual(sorted(path.name for path in ledger.iterdir()), ['records.json', 'records.lock'])
