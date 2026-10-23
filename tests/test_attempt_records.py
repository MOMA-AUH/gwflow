"""Storage guarantees at the internal attempt-record boundary."""

from concurrent.futures import ThreadPoolExecutor
import json
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
        self.attempt = {"owner": "c" * 32, "task": "task", "attempt": "a" * 32, "operation": "b" * 32}
        self.records = AttemptRecords(self.task_directory, self.attempt)
        self.records.initialize()

    def test_consolidated_document_is_authoritative_and_late_writes_stay_consolidated(self):
        self.records.publish("inputs.json", {"inputs": {"source": "stale"}})
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        state_path = root / "record-state.json"
        state = json.loads(state_path.read_text())
        state.update(phase="compacted", revision=state["revision"] + 1,
                     completion={"operation": self.attempt["operation"], "state": "pending"},
                     records={"inputs.json": {"inputs": {"source": "accepted"}},
                              "admissions/first/intent.json": {"submission": "first"}})
        state_path.write_text(json.dumps(state))
        self.assertEqual(self.records.read("inputs.json"), {"inputs": {"source": "accepted"}})
        self.records.publish("admissions/first/ack.json", {"job_id": 17}, replace=False)
        self.assertEqual(self.records.read("admissions/first/ack.json"), {"job_id": 17})
        self.assertEqual(self.records.names("admissions"), ["first"])
        self.assertFalse((root / "admissions").exists())
        updated = json.loads(state_path.read_text())
        self.assertEqual(updated["phase"], "compacted")
        self.assertEqual(updated["completion"], state["completion"])
        self.assertEqual(updated["revision"], state["revision"] + 1)
        self.assertEqual(updated["records"]["inputs.json"], state["records"]["inputs.json"])

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

    def test_bounded_observation_refreshes_after_another_writer_changes_a_record(self):
        from record_fixtures import consolidate
        self.records.publish("inputs.json", {"value": "before"})
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        consolidate(self.records, root)
        peer = AttemptRecords(self.task_directory, self.attempt)
        with _observations.planning_pass():
            with self.records.access():
                self.assertEqual(self.records.read("inputs.json"), {"value": "before"})
            peer.publish("inputs.json", {"value": "after"})
            with self.records.access():
                self.assertEqual(self.records.read("inputs.json"), {"value": "after"})

    def test_concurrent_writers_and_coherent_readers_preserve_all_updates(self):
        from record_fixtures import consolidate
        self.records.publish("left.json", {"revision": 0})
        self.records.publish("right.json", {"revision": 0})
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        consolidate(self.records, root)
        lock_inode = (root / "records.lock").stat().st_ino
        ready = Barrier(3)

        def paired_writer():
            peer = AttemptRecords(self.task_directory, self.attempt)
            ready.wait(timeout=5)
            for revision in range(1, 20):
                with peer.access(write=True):
                    peer.publish("left.json", {"revision": revision})
                    peer.publish("right.json", {"revision": revision})

        def acknowledgement_writer():
            peer = AttemptRecords(self.task_directory, self.attempt)
            ready.wait(timeout=5)
            for token in range(20):
                peer.publish(f"admissions/{token}/ack.json", {"job_id": token})

        def reader():
            peer = AttemptRecords(self.task_directory, self.attempt)
            ready.wait(timeout=5)
            with _observations.planning_pass():
                for _ in range(40):
                    with peer.access():
                        self.assertEqual(peer.read("left.json"), peer.read("right.json"))

        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(function) for function in (paired_writer, acknowledgement_writer, reader)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(self.records.read("left.json"), {"revision": 19})
        self.assertEqual(len(self.records.names("admissions")), 20)
        self.assertEqual((root / "records.lock").stat().st_ino, lock_inode)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["record-state.json", "records.lock"])

    def test_all_authoritative_phases_refuse_damaged_documents_and_ignore_redundant_files(self):
        from record_fixtures import consolidate
        self.records.publish("inputs.json", {"accepted": True})
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        consolidate(self.records, root, remove=False)
        path = root / "record-state.json"
        original = json.loads(path.read_text())
        for phase in ("consolidating", "compacted", "restoring"):
            with self.subTest(phase=phase):
                state = {**original, "phase": phase}
                path.write_text(json.dumps(state))
                (root / "inputs.json").write_text('{"accepted": false}')
                self.assertEqual(self.records.read("inputs.json"), {"accepted": True})
                self.records.publish("inputs.json", {"accepted": True, "late": True})
                self.assertEqual(json.loads(path.read_text())["phase"], phase)
        for damaged in ("{broken", "null", json.dumps({**original, "owner": "0" * 32}),
                        json.dumps({**original, "task": "someone_else"}),
                        json.dumps({**original, "attempt": "0" * 32}),
                        json.dumps({**original, "schema": True}),
                        json.dumps({**original, "phase": "unknown"}),
                        json.dumps({**original, "revision": -1}),
                        json.dumps({**original, "records": {"../outside": {}}})):
            with self.subTest(damaged=damaged):
                path.write_text(damaged)
                for observe in (lambda: self.records.read("inputs.json"), self.records.names,
                                lambda: self.records.exists("inputs.json"),
                                lambda: self.records.publish("inputs.json", {"overwrite": True})):
                    with self.assertRaises(WorkflowError):
                        observe()
        path.unlink()
        with self.assertRaises(WorkflowError):
            self.records.read("inputs.json")
        self.assertEqual(json.loads((root / "inputs.json").read_text()), {"accepted": False})

    def test_lock_and_document_path_substitutions_are_refused(self):
        self.records.publish("inputs.json", {"accepted": True})
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        for filename in ("records.lock", "record-state.json"):
            path = root / filename
            saved = path.with_suffix(".saved")
            path.rename(saved)
            try:
                for substitute in ("symlink", "copy", "fifo"):
                    if substitute == "copy" and filename == "record-state.json":
                        continue  # Atomic replacement is how documents are published.
                    with self.subTest(filename=filename, substitute=substitute):
                        if substitute == "symlink":
                            path.symlink_to(saved)
                        elif substitute == "copy":
                            path.write_bytes(saved.read_bytes())
                        else:
                            import os
                            os.mkfifo(path)
                        try:
                            with self.assertRaises(WorkflowError):
                                self.records.publish("inputs.json", {"overwrite": True})
                        finally:
                            path.unlink()
            finally:
                saved.rename(path)
        self.assertEqual(self.records.read("inputs.json"), {"accepted": True})

    def test_mutation_refuses_a_lock_replaced_during_the_session(self):
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        self.records.publish("inputs.json", {"accepted": True})
        lock = root / "records.lock"
        with self.records.access(write=True):
            lock.rename(root / "saved-lock")
            lock.touch()
            try:
                with self.assertRaises(WorkflowError):
                    self.records.publish("inputs.json", {"overwrite": True})
            finally:
                lock.unlink()
                (root / "saved-lock").rename(lock)
        self.assertEqual(self.records.read("inputs.json"), {"accepted": True})

    def test_failed_consolidated_publication_preserves_authority_and_other_records(self):
        from unittest.mock import patch
        from record_fixtures import consolidate
        self.records.publish("inputs.json", {"accepted": True})
        root = self.task_directory / "attempts" / self.attempt["attempt"]
        consolidate(self.records, root, remove=False)
        with patch("os.replace", side_effect=OSError("injected publication failure")):
            with self.assertRaises(WorkflowError):
                self.records.publish("admissions/late/ack.json", {"job_id": 7})
        self.assertFalse(self.records.exists("admissions/late/ack.json"))
        self.assertEqual(self.records.read("inputs.json"), {"accepted": True})
        self.records.publish("admissions/late/ack.json", {"job_id": 7})
        self.assertEqual(self.records.read("admissions/late/ack.json"), {"job_id": 7})
        self.assertFalse((root / "admissions").exists())
