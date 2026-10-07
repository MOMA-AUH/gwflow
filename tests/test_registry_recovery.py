"""Registry cache loss and recovery through ordinary installed CLI decisions."""

from collections import Counter
import os
import shlex
import shutil

import test_container_graphs
import test_inputs
import test_registry_images


class RegistryRecoveryTests(test_registry_images.RegistryTestCase):
    fault = test_inputs.ExternalInputTests.fault
    assert_blocked_without_changes = test_container_graphs.ContainerGraphTests.assert_blocked_without_changes

    def preview(self, reason):
        before = self.snapshot()
        for arguments in (("explain", "--details"), ("status", "--details"), ("run", "--dry-run", "--details")):
            output = self.cli(*arguments)
            expected = reason if reason in ("retry", "repair", "transfer") else "fresh attempt required"
            self.assertIn(expected if arguments[0] == "status" else reason, output)
            self.assertEqual(self.snapshot(), before)

    def test_failed_reacquisition_preserves_cleaned_results_and_restoration_reuses(self):
        self.configure_workflow(other=True)
        self.run_complete()
        attempt = self.attempt()
        self.cli("clean-work", "--delete")
        image = self.cached_images()[0]
        saved = self.work / "saved image.sif"
        image.rename(saved)
        self.options(fail=True)
        self.assert_blocked_without_changes("fixture registry unavailable")
        self.assertEqual(len(self.calls("pull")), 5)
        self.assertEqual(len(self.calls("exec")), 1)
        saved.rename(image)
        before = self.snapshot()
        self.assertRegex(self.cli("explain"), r"Task sample\s+Reuse\s+")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.attempt(), attempt)
        self.assertEqual(len(self.calls("exec")), 1)
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")

    def test_digest_reacquisition_compares_size_and_both_mtime_directions(self):
        self.configure(use_spec_hashes=False)
        reference = "docker://example.org/tools/demo@sha256:" + "a" * 64
        self.configure_workflow(reference)
        self.run_complete()
        image = self.cached_images()[0]
        for count, change in enumerate(("size", "forward", "backward"), start=2):
            with self.subTest(change=change):
                attempt, info = self.attempt(), image.stat()
                content = image.read_text() + ("padding" if change == "size" else "")
                mtime = info.st_mtime_ns + {"size": 0, "forward": 1_000_000_000, "backward": -1_000_000_000}[change]
                image.unlink()
                self.options(content=content, mtime_ns=mtime)
                self.preview("input metadata changed")
                self.assertEqual(image.stat().st_mtime_ns, mtime)
                self.assertEqual(self.attempt(), attempt)
                self.cli("run")
                self.settle()
                self.assertNotEqual(self.attempt(), attempt)
                self.assertEqual(len(self.calls("exec")), count)
                self.assertEqual((self.work / "results/sample/out.txt").read_text(), content)
        self.assertEqual([call["reference"] for call in self.calls("pull")], [reference] * 4)

    def test_matching_reacquired_observations_allow_cleaned_reuse(self):
        self.run_complete()
        self.cli("clean-work", "--delete")
        image = self.cached_images()[0]
        info = image.stat()
        before = self.snapshot()
        image.unlink()
        # Equal size/time at the same resolved path also documents the accepted
        # metadata-only limit: different bytes need not invalidate old work.
        self.options(content="changed image\n", mtime_ns=info.st_mtime_ns)
        self.assertRegex(self.cli("explain"), r"Task sample\s+Reuse\s+")
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(len(self.calls("exec")), 1)
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")

    def test_alias_retarget_and_reference_binding_require_fresh_attempts(self):
        self.configure(use_spec_hashes=False)
        self.run_complete()
        image = self.cached_images()[0]
        original = self.work / "original image.sif"
        shutil.copy2(image, original)
        attempt = self.attempt()
        image.unlink()
        image.symlink_to(original)
        self.preview("input metadata changed")
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt(), attempt)
        self.assertEqual(self.calls("exec")[-1]["image"], str(original))
        attempt = self.attempt()
        self.configure_workflow("docker://example.org/tools/demo:different-binding")
        self.options(mtime_ns=original.stat().st_mtime_ns)
        self.preview("changed declared structure")
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt(), attempt)
        self.assertEqual(len(self.calls("exec")), 3)
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")

    def check_worker_gate(self, job, gate, executions_before_gate):
        for mutation in ("delete", "change"):
            with self.subTest(mutation=mutation):
                for suffix in ("held", "release"):
                    (self.work / f"{gate}-{suffix}").unlink(missing_ok=True)
                before_exec = len(self.calls("exec"))
                env = self.fault(job=job, **{f"gate_after_{gate}": True})
                self.cli("-b", "recovery_fixture", "run", "--force", env=env)
                self.wait_for(lambda: (self.work / f"{gate}-held").exists())
                image = self.cached_images()[0]
                pulls = len(self.calls("pull"))
                try:
                    if mutation == "delete":
                        image.unlink()
                    else:
                        info = image.stat()
                        os.utime(image, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))
                finally:
                    (self.work / f"{gate}-release").touch()
                self.settle()
                self.assertFalse((self.work / "results/sample/out.txt").exists())
                self.assertEqual(len(self.calls("pull")), pulls, "Workers must never acquire images")
                self.assertEqual(len(self.calls("exec")), before_exec + executions_before_gate)
                self.cli("run")
                self.settle()
                self.assertEqual(len(self.calls("pull")), pulls + (mutation == "delete"))
                self.assertEqual(len(self.calls("exec")), before_exec + executions_before_gate + 1)
                self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")
                self.assertRegex(self.cli("explain"), r"Task sample\s+Reuse\s+")

    def test_workers_recheck_images_after_baseline_without_acquisition(self):
        self.check_worker_gate("gwflow_prepare", "baseline", 0)

    def test_completion_rechecks_images_after_manifest_without_acquisition(self):
        self.check_worker_gate("gwflow_complete", "manifest", 1)

    def test_restored_image_allows_repair_and_interrupted_transfer_without_computation(self):
        self.run_complete()
        attempt = self.attempt()
        result = self.work / "results/sample/out.txt"
        info = result.stat()
        result.write_text("damaged retained result")
        image = self.cached_images()[0]
        saved = self.work / "saved image.sif"
        image.rename(saved)
        self.options(fail=True)
        self.assert_blocked_without_changes("fixture registry unavailable")
        saved.rename(image)
        self.preview("repair")
        self.cli("-b", "recovery_fixture", "run",
                 env=self.fault(job="gwflow_complete", crash_during_copy=True))
        self.settle()
        self.assertEqual(result.read_text(), "damaged retained result")
        image.rename(saved)
        self.assert_blocked_without_changes("fixture registry unavailable")
        saved.rename(image)
        self.preview("transfer")
        self.cli("run")
        self.settle()
        self.assertEqual(self.attempt(), attempt)
        self.assertEqual(result.read_text(), "fixture image\n")
        self.assertEqual(result.stat().st_mtime_ns, info.st_mtime_ns)
        self.assertEqual(len(self.calls("exec")), 1)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_changed_image_cannot_replace_uncertain_submission_until_admitted(self):
        self.run_complete()
        attempt = self.attempt()
        self.fault(gate_before_preparation=True)
        env = self.inject(job_fault="sample__gwflow_prepare", lose_tracking="sample__gwflow_prepare")
        self.cli("-b", "recovery_fixture", "run", "--force", env=env, success=False)
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        blocked_attempt = self.attempt()
        try:
            self.configure_workflow("docker://example.org/tools/demo:replacement-while-uncertain")
            self.assert_blocked_without_changes("unresolved submission")
        finally:
            (self.work / "preparation-release").touch()
        self.settle()
        self.preview("changed declared structure")
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt(), attempt)
        self.assertNotEqual(self.attempt(), blocked_attempt)
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(len(self.calls("exec")), 2)
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")


class RegistryGraphRecoveryTests(test_registry_images.RegistryTestCase):
    assert_blocked_without_changes = test_container_graphs.ContainerGraphTests.assert_blocked_without_changes
    detail = test_container_graphs.ContainerGraphTests.detail
    preview = RegistryRecoveryTests.preview

    def configure_workflow(self):
        super().configure_workflow()
        self.failure = self.work / "fail right"
        self.write_workflow()

    def write_workflow(self, *, consumer=True):
        trace = shlex.quote(str(self.work / "trace"))
        failure = shlex.quote(str(self.failure))
        declaration = "from gwflow import Task, Workflow, shell\ngwf = Workflow()\ntask = Task(inputs=[])\n"
        for branch in ("left", "right"):
            command = f"echo {branch} >> {trace}; printf {branch} > {branch}.txt"
            if branch == "right":
                command += f"; if [ -e {failure} ]; then exit 7; fi"
            declaration += (
                f"{branch} = task.target({branch!r}, inputs=[], outputs=['{branch}.txt'], image={self.reference!r})\n"
                f"{branch} << {command!r}\n"
            )
        command = f"echo join >> {trace}; cat {{left}} {{right}} > joined.txt; touch -m -d @946684800 joined.txt"
        declaration += (
            "join = task.target('join', inputs=[left.output('left.txt'), right.output('right.txt')], outputs=['joined.txt'])\n"
            f"join << shell({command!r}, left=left.output('left.txt'), right=right.output('right.txt'))\n"
            "task.retain('value', source=join.output('joined.txt'), path='result.txt')\n"
            "producer = gwf.task_from_template('sample', task)\n"
            "task = Task(inputs=[])\n"
            "alone = task.target('compute', inputs=[], outputs=['out.txt'])\n"
            f"alone << {'echo independent >> ' + trace + '; printf independent > out.txt'!r}\n"
            "task.retain('value', source=alone.output('out.txt'), path='result.txt')\n"
            "gwf.task_from_template('independent', task)\n"
        )
        if consumer:
            declaration += (
                "task = Task(inputs=[producer.outputs['value']])\n"
                "read = task.target('read', inputs=task.inputs, outputs=['out.txt'])\n"
                f"read << shell({'echo consumer >> ' + trace + '; cat {source} > out.txt'!r}, source=producer.outputs['value'])\n"
                "task.retain('value', source=read.output('out.txt'), path='result.txt')\n"
                "gwf.task_from_template('consumer', task)\n"
            )
        (self.work / "workflow.py").write_text(declaration)

    def counts(self):
        return Counter((self.work / "trace").read_text().splitlines())

    def test_restored_image_retries_only_failed_branch(self):
        self.failure.touch()
        self.cli("run")
        self.settle()
        attempt = self.detail("sample", "Attempt")
        self.assertEqual(self.counts(), {"left": 1, "right": 1, "independent": 1})
        image = self.cached_images()[0]
        saved = self.work / "saved image.sif"
        image.rename(saved)
        self.options(fail=True)
        self.assert_blocked_without_changes("fixture registry unavailable")
        saved.rename(image)
        self.preview("retry")
        self.failure.unlink()
        self.cli("run")
        self.settle()
        self.assertEqual(self.detail("sample", "Attempt"), attempt)
        self.assertEqual(self.counts(), {"left": 1, "right": 2, "join": 1, "consumer": 1, "independent": 1})
        self.assertEqual((self.work / "results/consumer/result.txt").read_text(), "leftright")

    def test_changed_image_refreshes_successful_branch_of_partial_task(self):
        self.configure(use_spec_hashes=False)
        self.failure.touch()
        self.cli("run")
        self.settle()
        attempt = self.detail("sample", "Attempt")
        self.assertEqual(self.counts(), {"left": 1, "right": 1, "independent": 1})
        self.cached_images()[0].unlink()
        self.preview("input metadata changed")
        self.failure.unlink()
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.detail("sample", "Attempt"), attempt)
        self.assertEqual(self.counts(), {"left": 2, "right": 2, "join": 1, "consumer": 1, "independent": 1})
        self.assertEqual(len(self.calls("pull")), 2)

    def test_image_change_refreshes_consumers_even_with_equal_retained_metadata(self):
        self.configure(use_spec_hashes=False)
        self.run_complete()
        names = ("sample", "consumer", "independent")
        attempts = {name: self.detail(name, "Attempt") for name in names}
        result = self.work / "results/sample/result.txt"
        info = result.stat()
        image = self.cached_images()[0]
        image_info = image.stat()
        os.utime(image, ns=(image_info.st_atime_ns, image_info.st_mtime_ns - 1_000_000_000))
        before = self.snapshot()
        output = self.cli("explain")
        self.assertRegex(output, r"Task sample\s+Run\s+")
        self.assertRegex(output, r"Task consumer\s+Run\s+")
        self.assertRegex(output, r"Task independent\s+Reuse\s+")
        self.assertEqual(self.snapshot(), before)
        self.cli("run")
        self.settle()
        for name in ("sample", "consumer"):
            self.assertNotEqual(self.detail(name, "Attempt"), attempts[name])
        self.assertEqual(self.detail("independent", "Attempt"), attempts["independent"])
        self.assertEqual((result.stat().st_size, result.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))
        self.assertEqual(self.counts(), {"left": 2, "right": 2, "join": 2, "consumer": 2, "independent": 1})

    def test_image_change_respects_active_jobs_and_removed_active_consumers(self):
        self.run_complete()
        image = self.cached_images()[0]
        info = image.stat()
        os.utime(image, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))
        for prefix, reason in (("sample", "work blocks replacement"),
                               ("consumer", "Active consumers block replacement: consumer")):
            with self.subTest(prefix=prefix):
                if prefix == "consumer":
                    self.write_workflow(consumer=False)
                self.assert_blocked_without_changes(reason, backend=("-b", "recovery_fixture"),
                                                    env=self.inject(running_prefix=prefix))
        self.cli("run")
        self.settle()
        self.assertEqual(self.counts(), {"left": 2, "right": 2, "join": 2, "consumer": 1, "independent": 1})
        self.assertRegex(self.cli("explain"), r"Task sample\s+Reuse\s+")
