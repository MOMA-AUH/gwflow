"""Storage guarantees at the internal attempt-record boundary."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from gwf.exceptions import WorkflowError

from gwflow import _observations
from gwflow._records import AttemptRecords


class AttemptRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.task_directory = Path(temporary.name) / "task"
        self.attempt = {"attempt": "a" * 32, "operation": "b" * 32}
        self.records = AttemptRecords(self.task_directory, self.attempt)

    def test_concurrent_exclusive_creation_preserves_one_complete_record(self):
        ready = Barrier(2)

        def create(value):
            records = AttemptRecords(self.task_directory, self.attempt)
            ready.wait(timeout=5)
            try:
                records.publish("inputs.json", value, replace=False)
            except FileExistsError:
                return None
            return value

        first = {"inputs": {"source": "first"}, "producers": {"reference": "c" * 32}}
        second = {"inputs": {"source": "second"}, "producers": {"reference": "d" * 32}}
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(create, [first, second]))
        self.assertEqual(results.count(None), 1)
        winner = next(value for value in results if value is not None)
        self.assertEqual(self.records.read("inputs.json"), winner)
        self.assertEqual(self.records.names(), ["inputs.json"])

    def test_late_acknowledgement_is_visible_in_the_same_observation(self):
        peer = AttemptRecords(self.task_directory, self.attempt)
        peer.publish("admissions/first/intent.json", {"submission": "first"})
        with _observations.planning_pass():
            self.assertIsNone(self.records.read("admissions/first/ack.json"))
            self.assertEqual(self.records.names("admissions"), ["first"])
            peer.publish("admissions/first/ack.json", {"job_id": 17})
            self.assertEqual(self.records.read("admissions/first/ack.json"), {"job_id": 17})
            peer.publish("admissions/first/ack.json", {"job_id": 18})
            peer.publish("admissions/second/intent.json", {"submission": "second"})
            self.assertEqual(self.records.read("admissions/first/ack.json"), {"job_id": 18})
            self.assertEqual(self.records.names("admissions"), ["first", "second"])
        peer.publish("admissions/first/ack.json", {"job_id": 19})
        with _observations.planning_pass():
            self.assertEqual(self.records.read("admissions/first/ack.json"), {"job_id": 19})

    def test_failed_publication_preserves_existing_evidence(self):
        self.records.publish("completion.json", {"outputs": {"report": "checked"}})
        with self.assertRaises(TypeError):
            self.records.publish("completion.json", {"outputs": object()})
        self.assertEqual(self.records.read("completion.json"), {"outputs": {"report": "checked"}})
        self.assertEqual(self.records.names("operations/" + self.attempt["operation"]), ["completion.json"])

    def test_malformed_evidence_is_present_and_symlinked_history_is_refused(self):
        self.assertFalse(self.records.exists("inputs.json"))
        self.assertIsNone(self.records.read("inputs.json"))
        self.records.publish("inputs.json", {"inputs": {}})
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        (root / "inputs.json").write_text("{broken")
        self.assertTrue(self.records.exists("inputs.json"))
        self.assertIsNone(self.records.read("inputs.json"))
        outside = self.task_directory.parent / "outside"
        outside.mkdir()
        (outside / "ack.json").write_text('{"job_id": 42}')
        (root / "admissions").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(WorkflowError):
            self.records.names("admissions")
        with self.assertRaises(WorkflowError):
            self.records.read("admissions/ack.json")
        with self.assertRaises(WorkflowError):
            self.records.publish("admissions/ack.json", {"job_id": 99})
        self.assertEqual((outside / "ack.json").read_text(), '{"job_id": 42}')
