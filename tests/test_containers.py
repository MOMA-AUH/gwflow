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
import test_inputs


class ContainerAuthoringTests(unittest.TestCase):
    def test_image_requires_a_local_path_or_explicit_docker_reference(self):
        for image in ("", "oras://python:3.12", 123, b"image.sif", "bad\0path"):
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

    def test_image_cannot_resolve_into_managed_storage(self):
        for root in ("work", "results", ".gwf"):
            directory = self.work / root
            directory.mkdir(exist_ok=True)
            image = directory / "image.sif"
            image.touch()
            alias = self.work / "alias.sif"
            alias.symlink_to(image)
            try:
                for reference in (str(image), alias.name):
                    self.configure_workflow(reference)
                    output = self.cli("run", success=False)
                    self.assertIn("managed storage", output)
                    self.assertNotIn("Submitted target", output)
            finally:
                alias.unlink()


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerRuntimeTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject
    fault = test_inputs.ExternalInputTests.fault

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
                self.assertIn("input metadata changed", preview)
                self.assertIn(str(self.image), preview)
                self.assertIn("remove previous results", preview)
                self.cli("status", "--details")
                self.cli("run", "--dry-run")
                self.assertEqual(self.snapshot(), before)
                self.run_complete()
                self.assertNotEqual(self.attempt(), previous)
                self.assertNotIn("Submitted target", self.cli("run"))

    def test_changed_alias_refreshes_and_unavailable_image_preserves_results(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        previous = self.attempt()
        alias = self.work / "alias.sif"
        alias.symlink_to(self.image.name)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(self.image.name, alias.name))
        self.assertIn("changed declared structure", self.cli("explain"))
        self.run_complete()
        self.assertNotEqual(self.attempt(), previous)
        previous = self.attempt()
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

    def test_preparation_accepts_changed_image_and_launches_its_resolved_alias(self):
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
            replacement = self.work / "replacement.sif"
            shutil.copy2(self.image, replacement)
            alias.symlink_to(replacement)
            info = replacement.stat()
            os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns - 1000000000))
        finally:
            (self.work / "preparation-release").touch()
        self.finish()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "from-image\n")
        self.assertIn("reuse", self.cli("explain"))
        self.assertIn(str(replacement), self.cli("logs", "sample__compute", "--stderr", "--no-pager"))

    def test_image_retarget_after_baseline_prevents_computation(self):
        alias = self.work / "alias.sif"
        alias.symlink_to(self.image)
        replacement = self.work / "replacement.sif"
        shutil.copy2(self.image, replacement)
        ContainerPlanningTests.configure_workflow(self, alias.name)
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_baseline=True))
        self.wait_for(lambda: (self.work / "baseline-held").exists())
        try:
            alias.unlink()
            alias.symlink_to(replacement)
        finally:
            (self.work / "baseline-release").touch()
        self.settle()
        self.assertFalse(list((self.work / "work").rglob("out.txt")))
        self.assertFalse((self.work / "results/sample").exists())
        self.assertIn("inputs changed", self.cli("logs", "sample__compute", "--stderr", "--no-pager"))
        self.assertIn("fresh attempt", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "result")

    def test_changed_image_before_finishing_prevents_completion(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(job="gwflow_complete", gate_after_manifest=True))
        self.wait_for(lambda: (self.work / "manifest-held").exists())
        try:
            info = self.image.stat()
            os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))
        finally:
            (self.work / "manifest-release").touch()
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        self.assertIn("inputs changed", self.cli("logs", "sample__gwflow_complete", "--stderr", "--no-pager"))

    def test_preparation_retry_preserves_accepted_image_baseline(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_baseline=True))
        self.settle()
        attempt = self.attempt()
        before = self.snapshot()
        info = self.image.stat()
        os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        self.assertIn("fresh attempt", self.cli("run", "--dry-run"))
        self.assertEqual(self.snapshot(), before)
        os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.cli("run")
        self.settle()
        self.assertEqual(self.attempt(), attempt)
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "from-image\n")

    def test_implicit_image_is_not_staged_as_data(self):
        # An output can share the image's basename; implicit images do not
        # occupy the command's data-input namespace or staging directory.
        ContainerPlanningTests.configure_workflow(self, self.image.name, command=(
            "test ! -e 'prepared image.sif'; gwflow-image-tool > 'prepared image.sif'"
        ))
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("out.txt", self.image.name))
        self.run_complete()
        self.assertEqual((self.work / "results/sample" / self.image.name).read_text(), "from-image\n")

    def test_explicit_image_data_input_is_staged_and_bound(self):
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow, shell\ngwf = Workflow()\n"
            f"task = Task(inputs=[{self.image.name!r}])\n"
            f"target = task.target('compute', inputs=task.inputs, outputs=['out.txt'], image={self.image.name!r}, "
            f"stage_as={{'selected.sif': {self.image.name!r}}})\n"
            f"target << shell('test -L selected.sif; test -s {{source}}; printf staged > out.txt', source={self.image.name!r})\n"
            "task.retain('result', source=target.output('out.txt'), path='out.txt')\n"
            "gwf.task_from_template('sample', task)\n"
        )
        self.run_complete()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "staged")

    def test_interrupted_preparation_accepts_new_image_before_first_baseline(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_before_baseline=True))
        self.settle()
        attempt = self.attempt()
        info = self.image.stat()
        os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))
        self.assertIn("restart interrupted preparation", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertEqual(self.attempt(), attempt)
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "from-image\n")
        self.assertIn("reuse", self.cli("explain"))

    def test_image_changes_after_cleanup_still_require_fresh_attempt(self):
        self.configure(use_spec_hashes=False)
        self.run_complete()
        attempt = self.attempt()
        self.cli("clean-work", "--delete")
        info = self.image.stat()
        os.utime(self.image, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))
        before = self.snapshot()
        self.assertIn("fresh attempt", self.cli("explain"))
        self.assertEqual(self.snapshot(), before)
        self.run_complete()
        self.assertNotEqual(self.attempt(), attempt)

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

    def test_attempt_without_image_binding_is_rejected_without_deletion(self):
        self.run_complete()
        record = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/sample/attempts/*/attempt.json"))
        contents = json.loads(record.read_text())
        del contents["structure"]["targets"]["compute"]["image"]
        record.write_text(json.dumps(contents))
        before = self.snapshot()
        self.assertIn("unsupported managed attempt", self.cli("run", success=False))
        self.assertEqual(self.snapshot(), before)
