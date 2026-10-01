"""Image selection through public declarations and the installed workflow CLI."""

import json
import os
import re
import shutil
import unittest
from unittest.mock import patch

from gwf.exceptions import WorkflowError
from gwflow import Task

from support import FIXTURES, LocalBackendTestCase
import test_managed
import test_managed_recovery


class ContainerAuthoringTests(unittest.TestCase):
    def test_image_requires_a_local_path(self):
        for image in ("", "docker://python:3.12", 123, b"image.sif", "bad\0path"):
            with self.subTest(image=image), self.assertRaisesRegex(WorkflowError, "local SIF pathname"):
                Task(inputs=[]).target("compute", inputs=[], outputs=["out"], image=image)


class ContainerPlanningTests(LocalBackendTestCase):
    def configure_workflow(self, image="missing image.sif", command="printf result > out.txt"):
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[])\n"
            f"target = task.target('compute', inputs=[], outputs=['out.txt'], image={image!r})\n"
            f"target << {command!r}\n"
            "task.retain('result', source=target.output('out.txt'), path='out.txt')\n"
            "gwf.task_from_template('sample', task)\n"
        )

    def test_unavailable_image_blocks_all_frontend_decisions_without_storage(self):
        for arguments, success in ((('explain',), True), (('status', '--details'), True),
                                   (('run', '--dry-run'), False), (('run',), False)):
            with self.subTest(arguments=arguments):
                output = self.cli(*arguments, success=success)
                self.assertIn('blocked', output)
                self.assertIn('compute', output)
                self.assertIn('missing image.sif', output)
                self.assertFalse((self.work / 'work').exists())
                self.assertFalse((self.work / 'results').exists())

    def test_custom_executor_on_container_or_lifecycle_is_rejected_before_submission(self):
        image = self.work / "present.sif"
        image.touch()
        for setting in ("workflow", "task", "target", "preparation", "completion"):
            with self.subTest(setting=setting):
                self.configure_workflow(image.name)
                path = self.work / "workflow.py"
                source = "from gwf.executors import Conda\n" + path.read_text()
                if setting == "workflow":
                    source = source.replace("Workflow()", "Workflow(executor=Conda('unused'))")
                elif setting == "task":
                    source = source.replace("Task(inputs=[])", "Task(inputs=[], executor=Conda('unused'))")
                elif setting == "target":
                    source = source.replace("image=", "executor=Conda('unused'), image=")
                else:
                    source = source.replace("Workflow()", f"Workflow({setting}_defaults={{'executor': Conda('unused')}})")
                path.write_text(source)
                output = self.cli("run", "--dry-run", success=False)
                self.assertIn("Bash executor", output)
                self.assertFalse((self.work / "work").exists())


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerRuntimeTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject

    def setUp(self):
        with patch.dict(os.environ, {"PYTHONPATH": "/host/environment/pollution"}):
            super().setUp()

    def configure_workflow(self):
        self.image = self.work / "prepared image.sif"
        shutil.copyfile(os.environ["GWFLOW_TEST_SIF"], self.image)
        ContainerPlanningTests.configure_workflow(self, self.image.name, command=(
            'test -z "${PYTHONPATH:-}"; test -d "$TMPDIR"; '
            'echo scratch > "$TMPDIR/temporary"; gwflow-image-tool > out.txt'
        ))

    def test_image_software_runs_with_private_scratch_and_reuses_after_cleanup(self):
        self.run_complete()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "from-image\n")
        scratch = list((self.work / "work").rglob("temporary"))
        self.assertEqual(len(scratch), 1)
        self.assertEqual(scratch[0].read_text(), "scratch\n")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.cli("clean-work", "--delete")
        self.assertFalse(scratch[0].exists())
        self.assertNotIn("Submitted target", self.cli("run"))

    def attempt(self):
        return re.search(r"Attempt: (\w+)", self.cli("explain", "--details")).group(1)

    def snapshot(self):
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for root in (self.work / ".gwf/gwflow", self.work / "work", self.work / "results")
                for path in root.rglob("*") if path.is_file()}

    def test_image_metadata_changes_require_fresh_attempt_without_command_tracking(self):
        self.configure(use_spec_hashes=False)
        self.run_complete()
        for change in ("size", "mtime", "path"):
            with self.subTest(change=change):
                previous = self.attempt()
                info = self.image.stat()
                if change == "size":
                    with self.image.open("ab") as stream:
                        stream.write(b"padding")
                    os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns))
                elif change == "mtime":
                    os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns - 1000000000))
                else:
                    replacement = self.work / "replacement.sif"
                    shutil.copy2(self.image, replacement)
                    self.image.unlink()
                    self.image.symlink_to(replacement)
                before = self.snapshot()
                preview = self.cli("explain")
                self.assertIn("changed images: compute", preview)
                self.assertIn("remove previous results", preview)
                self.cli("status", "--details")
                self.cli("run", "--dry-run")
                self.assertEqual(self.snapshot(), before)
                self.run_complete()
                self.assertNotEqual(self.attempt(), previous)
                self.assertNotIn("Submitted target", self.cli("run"))

    def test_equivalent_alias_reuses_and_unavailable_image_preserves_results(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        previous = self.attempt()
        alias = self.work / "alias.sif"
        alias.symlink_to(self.image.name)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(self.image.name, alias.name))
        self.assertNotIn("Submitted target", self.cli("run"))
        saved = self.work / "saved.sif"
        self.image.rename(saved)
        before = self.snapshot()
        for arguments, success in ((('explain',), True), (('status', '--details'), True),
                                   (('run', '--dry-run'), False), (('run',), False)):
            output = self.cli(*arguments, success=success)
            self.assertIn("image unavailable", output)
            self.assertIn("compute", output)
            self.assertEqual(self.snapshot(), before)
        saved.rename(self.image)
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(self.attempt(), previous)

    def test_image_observation_is_frontend_only_and_launch_uses_resolved_alias(self):
        alias = self.work / "alias.sif"
        alias.symlink_to(self.image.name)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(self.image.name, alias.name))
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_before_preparation": True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="sample__gwflow_prepare"))
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        try:
            alias.unlink()
            alias.symlink_to("does-not-exist.sif")
            info = self.image.stat()
            os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns - 1000000000))
        finally:
            (self.work / "preparation-release").touch()
        self.finish()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "from-image\n")
        self.assertIn("image unavailable", self.cli("explain"))

    def test_failed_commands_and_invalid_outputs_never_establish_completion(self):
        commands = (
            "echo partial > out.txt; exit 7",
            "echo partial > out.txt; gwflow_deliberately_missing_command",
            "true",
            "mkdir out.txt",
            "ln -s /etc/passwd out.txt",
        )
        for command in commands:
            with self.subTest(command=command):
                ContainerPlanningTests.configure_workflow(self, self.image.name, command=command)
                self.cli("run", "--force")
                self.settle()
                self.assertFalse((self.work / "results/sample/out.txt").exists())
                self.assertIn(": retry;", self.cli("explain"))
                log = self.cli("logs", "sample__compute", "--stderr", "--no-pager")
                self.assertIn(str(self.image), log)
                self.assertIn("target compute", log)

    def test_unusable_image_fails_without_host_fallback(self):
        self.image.write_text("not a SIF")
        ContainerPlanningTests.configure_workflow(self, self.image.name)
        self.cli("run")
        self.settle()
        self.assertFalse((self.work / "results/sample/out.txt").exists())
        self.assertIn("image", self.cli("logs", "sample__compute", "--stderr", "--no-pager"))

    def test_missing_apptainer_fails_but_host_command_does_not_need_it(self):
        empty_path = self.work / "empty-bin"
        empty_path.mkdir()
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"execution_path": str(empty_path)}))
        ContainerPlanningTests.configure_workflow(self, self.image.name)
        env = self.inject(job_fault="sample__compute")
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.settle()
        self.assertFalse((self.work / "results/sample/out.txt").exists())
        log = self.cli("logs", "sample__compute", "--stderr", "--no-pager")
        self.assertIn("launch failed", log)
        self.assertIn("apptainer", log)
        ContainerPlanningTests.configure_workflow(self, None)
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.settle()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "result")

    def test_older_attempt_without_image_evidence_is_rejected_without_deletion(self):
        self.run_complete()
        record = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/sample/attempts/*/attempt.json"))
        contents = json.loads(record.read_text())
        del contents["images"]
        record.write_text(json.dumps(contents))
        before = self.snapshot()
        self.assertIn("unsupported managed attempt", self.cli("run", success=False))
        self.assertEqual(self.snapshot(), before)
