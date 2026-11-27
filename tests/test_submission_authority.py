"""Submission coordination across independent installed CLI processes on one frontend."""

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from support import FIXTURES, GWFLOW, TASK_FACTORY


class SubmissionAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        metadata = self.root / "recording-1.dist-info"
        metadata.mkdir()
        (metadata / "entry_points.txt").write_text("[gwf.backends]\nrecording = recording_backend:setup\n")
        self.acceptances = self.root / "accepted"
        self.env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, (self.root, FIXTURES))),
                    "GWFLOW_TEST_ACCEPTANCES": str(self.acceptances)}

    def workflow(self, directory, storage, name="sample", filename="workflow.py"):
        directory.mkdir(exist_ok=True)
        path = directory / filename
        path.write_text(TASK_FACTORY + "from gwflow import Workflow\n"
                        f"gwf = Workflow(working_dir={str(storage)!r})\n"
                        "task = empty_task(inputs=[])\n"
                        "job = task.target('compute', inputs=[], outputs=['out'])\n"
                        "job << 'touch out'\n"
                        "task.retain('output', source=job.output('out'), path='out')\n"
                        f"gwf.task(task, alias={name!r})\n")
        return path

    def command(self, path, *args):
        return [GWFLOW, "--file", str(path), "--backend", "recording", *args]

    def cli(self, path, *args, success=True):
        result = subprocess.run(self.command(path, *args), cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=15)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, success, output)
        return output

    def assert_accepted(self, *tasks):
        actual = [name.rsplit("__", 2)[0] for name in self.acceptances.read_text().splitlines()]
        self.assertCountEqual(actual, [f"{task}__{job}" for task in tasks
                                       for job in ("gwflow_prepare", "compute", "gwflow_complete")])

    def test_inspection_leaves_first_submission_free_to_select_its_directory(self):
        storage = self.root / "data"
        first = self.workflow(self.root / "first", storage)
        second = self.workflow(self.root / "second", storage)
        self.cli(first, "status", "--json")
        self.cli(first, "run", "--dry-run")
        self.cli(second, "run")
        self.assert_accepted("sample")
        for flags in (("--dry-run",), ()):
            output = self.cli(first, "run", *flags, success=False)
            self.assertIn(str(second.parent), output)
            self.assertIn("scheduler directory", output)
        alias = self.root / "directory-link"
        alias.symlink_to(second.parent, target_is_directory=True)
        self.cli(alias / second.name, "run")
        next_task = self.workflow(second.parent, storage, "next", "next.py")
        self.cli(alias / next_task.name, "run")
        self.assert_accepted("sample", "next")

    def test_first_submission_after_upgrade_preserves_existing_owner(self):
        storage = self.root / "data"
        original = self.workflow(self.root / "original", storage)
        self.cli(original, "run")
        # Older runs have the same ownership record and no scheduler record.
        records = storage / ".gwf/gwflow"
        (records / "scheduler.json").unlink()
        path = records / "owner.json"
        before = path.stat(), path.read_bytes()
        second = self.workflow(original.parent, storage, "other", "other.py")
        self.cli(second, "run", "--dry-run")
        self.assertFalse((records / "scheduler.json").exists())
        self.cli(second, "run")
        elsewhere = self.workflow(self.root / "alternate", storage, "other")
        self.assertIn(str(original.parent), self.cli(elsewhere, "run", success=False))
        self.cli(original, "run")
        self.assert_accepted("sample", "other")
        self.assertEqual(path.read_bytes(), before[1])
        self.assertEqual(path.stat().st_ino, before[0].st_ino)
        self.assertEqual(path.stat().st_mtime_ns, before[0].st_mtime_ns)

    def launch(self, path, *args, phase=None):
        output = tempfile.TemporaryFile(mode="w+")
        self.addCleanup(output.close)
        env = self.env.copy()
        if phase:
            env.update(GWFLOW_TEST_PHASE=phase, GWFLOW_TEST_GATE=str(self.root / "gate"))
        process = subprocess.Popen(self.command(path, *args), cwd=self.root, env=env,
                                   stdout=output, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(process.wait, timeout=5)
        self.addCleanup(process.kill)
        return process, output

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for the coordinated CLI command")
            time.sleep(0.02)

    def output(self, stream):
        # Reopen the descriptor independently of the child's write position.
        return os.pread(stream.fileno(), os.fstat(stream.fileno()).st_size, 0).decode()

    def wait_for_guard(self, command):
        process, stream = command
        self.wait_for(lambda: "Waiting for frontend submission bookkeeping" in self.output(stream)
                      or process.poll() is not None)
        self.assertIsNone(process.poll(), self.output(stream))

    def finished(self, command, success=True):
        process, stream = command
        process.wait(timeout=10)
        output = self.output(stream)
        self.assertEqual(process.returncode == 0, success, output)
        return output

    def competing_submissions(self, phase, *, symlink=False):
        first = self.workflow(self.root / "first", self.root / "data")
        alternate = self.root / "alternate"
        alternate.mkdir()
        if symlink:
            second = alternate / "workflow.py"
            second.symlink_to(first)
        else:
            second = self.workflow(alternate, self.root / "data")
        owner = self.launch(first, "run", phase=phase)
        self.wait_for((self.root / "gate.held").exists)
        contender = self.launch(second, "run")
        self.wait_for_guard(contender)
        (self.root / "gate.release").touch()
        self.finished(owner)
        self.assertIn(str(first.parent), self.finished(contender, success=False))
        self.assert_accepted("sample")
        self.assertIn(str(first.parent), self.cli(second, "run", "--dry-run", success=False))

    def test_overlapping_first_runs_select_one_scheduler(self):
        self.competing_submissions("planning")

    def test_workflow_file_symlink_coordinates_after_attempt_initialization(self):
        self.competing_submissions("submission", symlink=True)

    def test_shared_scheduler_serializes_tracking_while_independent_run_proceeds(self):
        scheduler = self.root / "shared"
        first = self.workflow(scheduler, self.root / "data1", "first")
        second = self.workflow(scheduler, self.root / "data2", "second", "second.py")
        third = self.workflow(self.root / "independent", self.root / "data3", "third")
        owner = self.launch(first, "run", phase="tracking")
        self.wait_for((self.root / "gate.held").exists)
        contender = self.launch(second, "run")
        self.wait_for_guard(contender)
        self.cli(third, "run")
        self.assert_accepted("first", "third")
        (self.root / "gate.release").touch()
        self.finished(owner)
        self.finished(contender)
        self.assert_accepted("first", "second", "third")
        self.cli(first, "run")
        self.cli(second, "run")
        self.assert_accepted("first", "second", "third")

    def test_interrupted_planning_releases_reversed_directory_pair(self):
        a, b = self.root / "a", self.root / "b"
        first = self.workflow(a, b, "first")
        reversed_workflow = self.workflow(b, a, "second")
        owner = self.launch(first, "run", phase="planning")
        self.wait_for((self.root / "gate.held").exists)
        contender = self.launch(reversed_workflow, "run")
        self.wait_for_guard(contender)
        owner[0].kill()
        self.finished(owner, success=False)
        self.finished(contender)
        self.cli(first, "run")
        self.assert_accepted("first", "second")

    def test_recovery_and_ordinary_retry_wait_for_an_interrupted_submitter(self):
        directory = self.root / "owner"
        first = self.workflow(directory, directory)
        second = self.workflow(self.root / "alternate", directory)
        owner = self.launch(first, "run", phase="submission")
        self.wait_for((self.root / "gate.held").exists)
        recovery = self.launch(second, "recover", "sample")
        self.wait_for_guard(recovery)
        owner[0].kill()
        self.finished(owner, success=False)
        self.assertIn("No unresolved submissions", self.finished(recovery))
        self.assertIn(str(directory), self.cli(second, "run", success=False))
        self.cli(first, "run")
        self.cli(first, "run")
        self.assert_accepted("sample")

    def test_directory_aliases_that_share_one_lock_can_submit(self):
        scheduler, storage = self.root / "scheduler", self.root / "data"
        path = self.workflow(scheduler, storage)
        # Two names for the same inode model the lock visible through two
        # bind mounts, without requiring mount privileges in the test suite.
        for directory in (scheduler, storage):
            (directory / ".gwf").mkdir(parents=True)
        lock = scheduler / ".gwf/gwflow-submission.lock"
        lock.touch()
        (storage / ".gwf" / lock.name).hardlink_to(lock)
        self.cli(path, "run")
        self.assert_accepted("sample")
