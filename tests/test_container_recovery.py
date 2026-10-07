"""Image-aware failure, retry and result repair through installed CLI workflows."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import unittest

from support import FIXTURES, LocalBackendTestCase
import test_containers
import test_container_graphs
import test_managed
import test_managed_recovery


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerRecoveryTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject
    snapshot = test_containers.ContainerRuntimeTests.snapshot
    assert_blocked_without_changes = test_container_graphs.ContainerGraphTests.assert_blocked_without_changes

    def configure_workflow(self):
        self.image = self.work / "prepared image.sif"
        shutil.copyfile(os.environ["GWFLOW_TEST_SIF"], self.image)
        self.failure = self.work / "deployment-problem"
        self.failure.touch()
        trace, failure = shlex.quote(str(self.work / "trace")), shlex.quote(str(self.failure))
        right = (f'echo right >> {trace}; printf "PLACE=%s|%s\\n" "$PWD" "$TMPDIR"; '
                 'test ! -e abandoned; test ! -e "$TMPDIR/abandoned"; printf partial > right.txt; '
                 f'if [ -e {failure} ]; then touch abandoned "$TMPDIR/abandoned"; exit 7; fi; printf right > right.txt')
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow, shell\ngwf = Workflow()\ntask = Task(inputs=[])\n"
            f"left = task.target('left', inputs=[], outputs=['left.txt'], image={str(self.image)!r})\n"
            f"left << {'echo left >> ' + trace + '; printf left > left.txt'!r}\n"
            f"right = task.target('right', inputs=[], outputs=['right.txt'], image={str(self.image)!r})\n"
            f"right << {right!r}\n"
            f"join = task.target('join', inputs=[left.output('left.txt'), right.output('right.txt')], outputs=['joined.txt'], image={str(self.image)!r})\n"
            f"join << shell({'echo join >> ' + trace + '; cat {left} {right} > {out}'!r}, left=left.output('left.txt'), right=right.output('right.txt'), out=join.output('joined.txt'))\n"
            "task.retain('value', source=join.output('joined.txt'), path='result.txt')\n"
            "producer = gwf.task_from_template('sample', task)\n"
        )

    def counts(self):
        return Counter((self.work / "trace").read_text().splitlines())

    def attempt(self):
        return re.search(r"Attempt: (\w+)", self.cli("explain", "sample", "--details")).group(1)

    def location(self):
        return next(line.removeprefix("PLACE=").split("|")
                    for line in self.cli("logs", "sample__right", "--no-pager").splitlines() if line.startswith("PLACE="))

    def preview(self, reason):
        before = self.snapshot()
        for arguments in (("explain", "--details"), ("status", "--details"), ("run", "--dry-run", "--details")):
            output = self.cli(*arguments)
            expected = reason if reason in ("retry", "repair", "transfer") else "fresh attempt required"
            self.assertIn(expected if arguments[0] == "status" else reason, output)
            self.assertEqual(self.snapshot(), before)

    def fault(self, **options):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps(options))
        return self.inject(job_fault="sample__gwflow_complete")

    def add_consumer(self):
        workflow = self.work / "workflow.py"
        command = f"echo consumer >> {shlex.quote(str(self.work / 'trace'))}; cat {{source}} > copy.txt"
        workflow.write_text(workflow.read_text() +
            "task = Task(inputs=[producer.outputs['value']])\n"
            f"read = task.target('read', inputs=task.inputs, outputs=['copy.txt'], image={str(self.image)!r})\n"
            f"read << shell({command!r}, source=producer.outputs['value'])\n"
            "task.retain('copy', source=read.output('copy.txt'), path='result.txt')\n"
            "gwf.task_from_template('consumer', task)\n")

    def test_ordinary_retry_preserves_successful_branch_and_uses_fresh_private_storage(self):
        self.cli("run")
        self.settle()
        self.assertEqual(self.counts(), {"left": 1, "right": 1})
        self.assertFalse((self.work / "results/sample/result.txt").exists())
        log = self.cli("logs", "sample__right", "--stderr", "--no-pager")
        self.assertIn("target right", log)
        self.assertIn(str(self.image), log)
        self.assertIn("exit 7", log)
        attempt, old = self.attempt(), self.location()
        self.failure.unlink()
        self.preview("retry")
        self.cli("run")
        self.settle()
        self.assertEqual(self.attempt(), attempt)
        self.assertNotEqual(self.location(), old)
        self.assertTrue(all(Path(path).exists() for path in old))
        self.assertEqual(self.counts(), {"left": 1, "right": 2, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_matching_images_allow_interrupted_repair_without_repeating_commands(self):
        self.failure.unlink()
        self.run_complete()
        attempt = self.attempt()
        result = self.work / "results/sample/result.txt"
        original = result.stat()
        result.write_text("damaged retained data")
        self.preview("repair")
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        self.assertEqual(result.read_text(), "damaged retained data")
        self.assertEqual(self.attempt(), attempt)
        self.preview("transfer")
        empty = self.work / "empty-bin"
        empty.mkdir()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(execution_path=str(empty)))
        self.settle()
        self.assertEqual(result.read_text(), "leftright")
        self.assertEqual(result.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(self.counts(), {"left": 1, "right": 1, "join": 1})
        self.assertEqual(self.attempt(), attempt)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_unavailable_image_blocks_repair_and_changed_image_requires_fresh_computation(self):
        self.configure(use_spec_hashes=False)
        self.failure.unlink()
        self.run_complete()
        attempt = self.attempt()
        result = self.work / "results/sample/result.txt"
        result.write_text("damaged retained data")
        saved = self.work / "saved.sif"
        self.image.rename(saved)
        self.assert_blocked_without_changes("image unavailable")
        saved.rename(self.image)
        self.preview("repair")
        info = self.image.stat()
        os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns - 1000000000))
        self.preview("input metadata changed")
        self.run_complete()
        self.assertNotEqual(self.attempt(), attempt)
        self.assertEqual(result.read_text(), "leftright")
        self.assertEqual(self.counts(), {"left": 2, "right": 2, "join": 2})

    def test_transfer_continuation_checks_image_again_on_worker(self):
        self.failure.unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_manifest=True))
        self.settle()
        attempt = self.attempt()
        saved = self.work / "saved.sif"
        self.image.rename(saved)
        self.assert_blocked_without_changes("image unavailable")
        saved.rename(self.image)
        self.preview("transfer")
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_before_preparation=True))
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        try:
            self.image.rename(saved)
        finally:
            (self.work / "preparation-release").touch()
        self.settle()
        self.assertFalse((self.work / "results/sample/result.txt").exists())
        self.assertIn("inputs unavailable", self.cli("logs", "sample__gwflow_complete", "--stderr", "--no-pager"))
        saved.rename(self.image)
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(self.attempt(), attempt)
        self.assertEqual(self.counts(), {"left": 1, "right": 1, "join": 1})
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_protected_dependents_block_retry_and_image_changes_refresh_successful_siblings(self):
        self.configure(use_spec_hashes=False)
        self.cli("run")
        self.settle()
        attempt = self.attempt()
        for setting in ("queued_prefix", "running_prefix"):
            with self.subTest(setting=setting):
                self.assert_blocked_without_changes("Active dependent work blocks retry: join",
                                                    backend=("-b", "recovery_fixture"),
                                                    env=self.inject(**{setting: "sample__join"}))
        saved = self.work / "saved.sif"
        self.image.rename(saved)
        self.assert_blocked_without_changes("image unavailable")
        saved.rename(self.image)
        self.preview("retry")
        info = self.image.stat()
        os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns - 1000000000))
        self.preview("input metadata changed")
        self.failure.unlink()
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt(), attempt)
        self.assertEqual(self.counts(), {"left": 2, "right": 2, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_repair_preserves_active_work_consumer_and_ownership_protections(self):
        self.failure.unlink()
        original = (self.work / "workflow.py").read_text()
        self.add_consumer()
        self.run_complete()
        (self.work / "workflow.py").write_text(original)
        result = self.work / "results/sample/result.txt"
        result.write_text("protected damaged result")
        before = self.snapshot()
        env = self.inject(running_prefix="sample")
        for arguments in (("explain", "--details"), ("status", "--details"), ("run", "--dry-run", "--details"), ("run", "--details")):
            output = self.cli("-b", "recovery_fixture", *arguments, env=env)
            self.assertIn("running" if arguments[0] == "status" else "queued/running work", output)
            self.assertNotIn("Submitted target", output)
            self.assertEqual(self.snapshot(), before)
        self.assert_blocked_without_changes("Active consumers block replacement: consumer",
                                            backend=("-b", "recovery_fixture"),
                                            env=self.inject(running_prefix="consumer"))
        manifest = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/sample/attempts/*/**/manifest.json"))
        manifest.write_text("{truncated")
        self.assert_blocked_without_changes("ownership")
        self.assertEqual(result.read_text(), "protected damaged result")
        self.assertEqual((self.work / "results/consumer/result.txt").read_text(), "leftright")

    def test_uncertain_consumer_admission_blocks_producer_repair(self):
        self.failure.unlink()
        self.run_complete()
        original = (self.work / "workflow.py").read_text()
        self.add_consumer()
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"crash_before_commit": True}))
        self.cli("-b", "recovery_fixture", "run", success=False,
                 env=self.inject(job_fault="consumer__read", lose_tracking="consumer__read"))
        self.settle()
        self.assertFalse((self.work / "results/consumer").exists())
        result = self.work / "results/sample/result.txt"
        result.write_text("protected after lost acknowledgement")
        (self.work / "workflow.py").write_text(original)
        self.assert_blocked_without_changes("Active consumers block replacement: consumer")
        self.assertEqual(result.read_text(), "protected after lost acknowledgement")
