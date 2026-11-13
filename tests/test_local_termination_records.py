"""Independent publishers share the durable Submission evidence boundary."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter

from gwf.backends.local import LocalStatus
from gwf.exceptions import WorkflowError
from gwflow import _files, _observations
from gwflow._local_termination import TerminationRecords


class LookupWork:
    """Count actual decoding, validation visits, and copies without replacing them."""

    def __init__(self):
        self.counts = Counter()

    def profile(self, frame, event, arg):
        if event != "call":
            return
        name, filename = frame.f_code.co_name, frame.f_code.co_filename
        if name == "loads" and filename.endswith("json/__init__.py"):
            self.counts["decodes"] += 1
        elif name == "<genexpr>" and filename.endswith("gwflow/_local_termination.py"):
            self.counts["validation_visits"] += 1
        elif name == "deepcopy" and filename.endswith("copy.py"):
            self.counts["copies"] += 1

    def __enter__(self):
        self.previous = sys.getprofile()
        sys.setprofile(self.profile)
        return self.counts

    def __exit__(self, *error):
        sys.setprofile(self.previous)


class TerminationRecordTests(unittest.TestCase):
    def ledger(self, root, **submissions):
        records = TerminationRecords(root)
        records.root.mkdir(parents=True, exist_ok=True)
        records.path.write_text(json.dumps({"version": 1, "submissions": submissions}))
        return records

    def test_lookup_work_grows_with_history_plus_lookups(self):
        with tempfile.TemporaryDirectory() as temporary:
            records = TerminationRecords(temporary)
            records.root.mkdir(parents=True)
            for history in (32, 128, 512):
                records.path.write_text(json.dumps({"version": 1, "submissions": {
                    f"submission-{number}": "COMPLETED" for number in range(history)}}))
                for lookups in (1, 16, 64):
                    with self.subTest(history=history, lookups=lookups):
                        with _observations.planning_pass(), LookupWork() as work:
                            for number in range(lookups):
                                # Frontend fallback creates a reader for each lookup.
                                self.assertEqual(TerminationRecords(temporary).read(
                                    f"submission-{number % history}"), LocalStatus.COMPLETED)
                        self.assertEqual(work["decodes"], 1)
                        self.assertGreater(work["validation_visits"], 0)
                        self.assertLessEqual(work["validation_visits"], history + 1)
                        self.assertLessEqual(work["copies"], 8 * (history + lookups))

    def test_changed_evidence_is_current_within_and_across_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            records = self.ledger(temporary, original="COMPLETED")
            for same_observation in (True, False):
                for change in ("replace", "modify", "remove", "corrupt"):
                    with self.subTest(same_observation=same_observation, change=change):
                        self.ledger(temporary, original="COMPLETED")
                        def change_evidence():
                            if change == "replace":
                                _files.publish(records.path, {"version": 1, "submissions": {"new": "FAILED"}})
                            elif change == "modify":
                                before = records.path.stat()
                                records.path.write_text(json.dumps({"version": 1, "submissions": {"new": "FAILED"}}))
                                os.utime(records.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                            elif change == "remove":
                                records.path.unlink()
                            else:
                                records.path.write_text("{broken")

                        def assert_changed():
                            self.assertEqual(records.read("original"), LocalStatus.UNKNOWN)
                            expected = LocalStatus.FAILED if change in ("replace", "modify") else LocalStatus.UNKNOWN
                            self.assertEqual(records.read("new"), expected)
                            # A repair or later publication is visible immediately.
                            records.path.write_text(json.dumps({"version": 1, "submissions": {"original": "KILLED"}}))
                            self.assertEqual(records.read("original"), LocalStatus.KILLED)

                        with _observations.planning_pass():
                            self.assertEqual(records.read("original"), LocalStatus.COMPLETED)
                            change_evidence()
                            if same_observation:
                                assert_changed()
                        if not same_observation:
                            with _observations.planning_pass():
                                assert_changed()

    def test_each_published_revision_is_processed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            for history in (16, 64):
                records = self.ledger(temporary, **{f"job-{i}": "COMPLETED" for i in range(history)})
                for lookups in (4, 16):
                    with self.subTest(history=history, lookups=lookups):
                        with _observations.planning_pass(), LookupWork() as work:
                            for state in ("FAILED", "CANCELLED", "KILLED"):
                                records.record("selected", LocalStatus[state])
                                for _ in range(lookups):
                                    self.assertEqual(records.read("selected"), LocalStatus[state])
                        # Three serialized writers read and validate history;
                        # readers add one decode/validation per new revision.
                        self.assertLessEqual(work["decodes"], 6)
                        self.assertLessEqual(work["validation_visits"], 6 * (history + 2))
                        self.assertLessEqual(work["copies"], 24 * (history + lookups))

    def test_validation_and_parsing_end_with_each_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            records = self.ledger(temporary, first="CANCELLED")
            for _ in range(3):
                with _observations.planning_pass(), LookupWork() as work:
                    for _ in range(4):
                        self.assertEqual(records.read("first"), LocalStatus.CANCELLED)
                self.assertEqual(work["decodes"], 1)
                self.assertEqual(work["validation_visits"], 2)

    def test_missing_invalid_nonterminal_and_other_submission_are_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            records = self.ledger(temporary, exact="COMPLETED")
            invalid = ([], {}, {"version": True, "submissions": {"exact": "COMPLETED"}},
                       {"version": 2, "submissions": {"exact": "COMPLETED"}},
                       {"version": 1, "submissions": []},
                       {"version": 1, "submissions": {"exact": "COMPLETED", "other": "INVALID"}},
                       {"version": 1, "submissions": {"exact": {"state": "COMPLETED"}}})
            with _observations.planning_pass():
                self.assertEqual(records.read("exact"), LocalStatus.COMPLETED)
                self.assertEqual(records.read("other"), LocalStatus.UNKNOWN)
                for record in invalid:
                    with self.subTest(record=record):
                        _files.publish(records.path, record)
                        self.assertEqual(records.read("exact"), LocalStatus.UNKNOWN)
                for state in LocalStatus:
                    _files.publish(records.path, {"version": 1, "submissions": {"exact": state.name}})
                    expected = state if state.name in ("COMPLETED", "FAILED", "CANCELLED", "KILLED") else LocalStatus.UNKNOWN
                    self.assertEqual(records.read("exact"), expected)

    def test_selected_mutable_values_cannot_modify_cached_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            records = self.ledger(temporary, exact="COMPLETED")
            with _observations.planning_pass():
                selected = _files.read_json(records.path, validate=TerminationRecords._valid,
                                            select=lambda record: record["submissions"])
                selected["exact"] = "FAILED"
                self.assertEqual(records.read("exact"), LocalStatus.COMPLETED)
                whole = _files.read_json(records.path)
                whole["submissions"]["exact"] = "KILLED"
                self.assertEqual(records.read("exact"), LocalStatus.COMPLETED)
                self.assertEqual(_files.read_json(records.path)["submissions"]["exact"], "COMPLETED")

    def test_cached_evidence_keeps_directory_and_file_identity_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            records = self.ledger(temporary, exact="COMPLETED")
            with _observations.planning_pass():
                self.assertEqual(records.read("exact"), LocalStatus.COMPLETED)
                displaced = records.root.with_name("displaced")
                records.root.rename(displaced)
                records.root.symlink_to(displaced, target_is_directory=True)
                with self.assertRaisesRegex(WorkflowError, "Managed directory changed"):
                    records.read("exact")
            records.root.unlink()
            displaced.rename(records.root)
            with _observations.planning_pass():
                self.assertEqual(records.read("exact"), LocalStatus.COMPLETED)
                moved = records.path.with_name("moved.json")
                records.path.rename(moved)
                records.path.symlink_to(moved)
                with self.assertRaisesRegex(WorkflowError, "Cannot read managed evidence"):
                    records.read("exact")

    def test_same_size_modification_with_restored_mtime_invalidates_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            records = self.ledger(temporary, exact="COMPLETED")
            before = records.path.stat()
            with _observations.planning_pass():
                self.assertEqual(records.read("exact"), LocalStatus.COMPLETED)
                # Filesystem timestamps can share a tick. Establish an observed
                # ctime change while preserving inode, size, and original mtime.
                changed = records.path.read_bytes().replace(b"COMPLETED", b"SUBMITTED")
                deadline = time.monotonic() + 2
                while True:
                    records.path.write_bytes(changed)
                    os.utime(records.path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    after = records.path.stat()
                    if after.st_ctime_ns != before.st_ctime_ns:
                        break
                    self.assertLess(time.monotonic(), deadline, "Filesystem did not advance ctime")
                    time.sleep(.01)
                self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                                 (before.st_ino, before.st_size, before.st_mtime_ns))
                self.assertEqual(records.read("exact"), LocalStatus.UNKNOWN)

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
