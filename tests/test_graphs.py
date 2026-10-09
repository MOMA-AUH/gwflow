"""Internal Task graphs and partial retries through public workflows."""

from collections import Counter
from pathlib import Path
import json
import shlex
import shutil

from support import TASK_FACTORY, FIXTURES, LocalBackendTestCase
import test_managed
import test_managed_recovery


class TaskGraphTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject
    slurm_environment = test_managed_recovery.ManagedCoordinationTests.slurm_environment

    def configure_workflow(self, *, right_command=None, left_command=None, right_outputs=("same.txt",)):
        trace = shlex.quote(str(self.work / "trace"))
        if left_command is None:
            left_command = 'echo left >> ' + trace + '; printf left > same.txt'
        if right_command is None:
            right_command = 'echo right >> ' + trace + '; printf right > same.txt'
        join_command = 'echo join >> ' + trace + '; cat {left} {right} > {out}'
        (self.work / "workflow.py").write_text(
            TASK_FACTORY + "from gwflow import Task, Workflow, shell\n"
            "gwf = Workflow()\n"
            "task = empty_task(inputs=[])\n"
            "left = task.target('left', inputs=[], outputs=['same.txt'])\n"
            f"left << {left_command!r}\n"
            f"right = task.target('right', inputs=[], outputs={list(right_outputs)!r})\n"
            f"right << {right_command!r}\n"
            "join = task.target('join', inputs=[left.output('same.txt'), right.output('same.txt')], outputs=['joined.txt'])\n"
            f"join << shell({join_command!r}, left=left.output('same.txt'), right=right.output('same.txt'), out=join.output('joined.txt'))\n"
            "task.retain('joined', source=join.output('joined.txt'), path='result.txt')\n"
            "gwf.task(task, alias='sample')\n"
        )

    def test_target_references_read_distinct_committed_output_sets(self):
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertCountEqual((self.work / "trace").read_text().splitlines(), ["left", "right", "join"])
        self.assertIn("Reuse", self.cli("explain"))

    def test_retry_preserves_successful_sibling_and_uses_fresh_execution_storage(self):
        failing = self.work / "fail-right"
        failing.touch()
        self.configure_workflow(right_command=(
            f"echo right >> {shlex.quote(str(self.work / 'trace'))}; "
            "printf 'LOCATION=%s|%s\n' \"$PWD\" \"$TMPDIR\"; printf partial > same.txt; "
            f"if [ -e {shlex.quote(str(failing))} ]; then printf abandoned > other.txt; exit 8; fi; test ! -e other.txt; printf fresh > other.txt; printf right > same.txt"
        ), right_outputs=("same.txt", "other.txt"))
        self.configure(use_spec_hashes=True)
        self.cli("run")
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        first_logs = self.cli("logs", "sample__right", "--no-pager")
        first_location = next(line for line in first_logs.splitlines() if line.startswith("LOCATION="))
        attempt_line = next(line for line in self.cli("explain", "--details").splitlines() if "Attempt:" in line)
        self.assertRegex(self.cli("explain"), r"Task sample\s+Retry\s+")
        failing.unlink()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("gwf = Workflow()", "gwf = Workflow(defaults={'cores': 3})"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":2, "join":1})
        second_logs = self.cli("logs", "sample__right", "--no-pager")
        second_location = next(line for line in second_logs.splitlines() if line.startswith("LOCATION="))
        self.assertNotEqual(first_location, second_location)
        for directory in first_location.removeprefix("LOCATION=").split("|"):
            self.assertTrue(Path(directory).is_dir())
        self.assertIn(attempt_line, self.cli("explain", "--details"))
        self.assertIn("Reuse", self.cli("explain"))

    def test_failed_branch_retries_while_unrelated_sibling_keeps_running(self):
        from gwf.backends.local import Client, LocalStatus
        held, release, failing = (self.work / name for name in ("left-held", "left-release", "fail-right"))
        failing.touch()
        trace = shlex.quote(str(self.work / "trace"))
        self.configure_workflow(
            left_command=f"echo left >> {trace}; touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf left > same.txt",
            right_command=f"echo right >> {trace}; if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt",
        )
        workflow = self.work / "workflow.py"
        declaration = workflow.read_text().split("join = task.target")[0]
        workflow.write_text(declaration +
            "done = task.target('done', inputs=[], outputs=['done.txt'])\n" +
            f"done << {'echo done >> ' + trace + '; printf done > done.txt'!r}\n" +
            "task.retain('right', source=right.output('same.txt'), path='result.txt')\n" +
            "gwf.task(task, alias='sample')\n")
        self.cli("run")
        self.wait_for(held.exists)
        def right_failed():
            with Client.connect(port=self.port) as client:
                return LocalStatus.FAILED in client.status().values()
        self.wait_for(right_failed)
        self.wait_for(lambda: "done" in (self.work / "trace").read_text().splitlines())
        try:
            for command in (("status",), ("explain",), ("run", "--dry-run")):
                output = self.cli_result(*command, "--details").stdout
                if command[0] == "status":
                    self.assertRegex(output, r"Task sample\s+failed\s+\d+/5")
                    self.assertIn("retry available", output)
                else:
                    self.assertIn("State: failed; Jobs completed:", output)
                    self.assertIn("Next action: Retry", output)
                self.assertRegex(output, r"left\s+running")
            failing.unlink()
            output = self.cli_result("run", "--details").stdout
            self.assertIn("State: failed; Jobs completed:", output)
            self.assertIn("Next action: Retry", output)
            self.assertRegex(output, r"left\s+running")
            self.wait_for(lambda: Counter((self.work / "trace").read_text().splitlines())["right"] == 2)
            self.assertEqual(Counter((self.work / "trace").read_text().splitlines())["left"], 1)
            self.assertFalse((self.work / "results/sample").exists())
        finally:
            release.touch()
        self.settle()
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "right")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":2, "done":1})

    def test_rejected_retry_admission_resumes_selected_generations(self):
        failing = self.work / "fail-right"
        failing.touch()
        trace = shlex.quote(str(self.work / "trace"))
        self.configure_workflow(right_command=f"echo right >> {trace}; if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt")
        self.cli("run")
        self.settle()
        attempt_line = next(line for line in self.cli("explain", "--details").splitlines() if "Attempt:" in line)
        failing.unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        self.assertIn("Continue", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":2, "join":1})
        self.assertIn(attempt_line, self.cli("explain", "--details"))

    def interrupted_target(self, fault):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({fault: True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="sample__right"))
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        self.assertRegex(self.cli("explain"), r"Task sample\s+Retry\s+")
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":2, "join":1})
        self.assertCountEqual([path.read_text() for path in (self.work / "work").rglob("same.txt")], ["left", "right", "right"])

    def test_interruption_before_output_commit_retries_in_fresh_staging(self):
        self.interrupted_target("crash_before_commit")

    def test_interruption_after_output_commit_does_not_adopt_unchecked_files(self):
        self.interrupted_target("crash_after_commit")

    def test_interruption_before_success_publication_retries_computation(self):
        self.interrupted_target("crash_before_success")

    def test_cycles_and_foreign_or_undeclared_target_references_fail_before_submission(self):
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        variants = [
            original.replace("gwf.task", "left.inputs.append(join.output('joined.txt'))\ngwf.task"),
            original.replace("right = task.target", "foreign = empty_task(inputs=[]).target('foreign', inputs=[], outputs=['data'])\nleft.inputs.append(foreign.output('data'))\nright = task.target"),
            original.replace("inputs=[left.output('same.txt'), right.output('same.txt')]", "inputs=[right.output('same.txt')]"),
        ]
        for declaration in variants:
            with self.subTest(declaration=declaration):
                workflow.write_text(declaration)
                self.cli("run", success=False)
                self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_replacing_an_upstream_execution_invalidates_successful_dependents(self):
        failing = self.work / "fail-right"
        failing.touch()
        trace = shlex.quote(str(self.work / "trace"))
        self.configure_workflow(
            left_command=f"echo left >> {trace}; printf left > same.txt; printf 'OUTPUT=%s/same.txt\n' \"$PWD\"",
            right_command=f"echo right >> {trace}; if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt",
        )
        workflow = self.work / "workflow.py"
        declaration = workflow.read_text()
        declaration = declaration.replace("join = task.target", "mid = task.target('mid', inputs=[left.output('same.txt')], outputs=['middle.txt'])\n" +
            f"mid << shell({'echo mid >> ' + trace + '; printf \"SOURCE=%s\\n\" {source}; cat {source} > {out}'!r}, source=left.output('same.txt'), out=mid.output('middle.txt'))\njoin = task.target")
        declaration = declaration.replace("inputs=[left.output('same.txt'), right.output('same.txt')]", "inputs=[mid.output('middle.txt'), right.output('same.txt')]")
        declaration = declaration.replace("left=left.output('same.txt')", "left=mid.output('middle.txt')")
        workflow.write_text(declaration)
        self.cli("run")
        self.settle()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines())["mid"], 1)
        logs = self.cli("logs", "sample__mid", "--no-pager")
        output = next(line.removeprefix("SOURCE=") for line in logs.splitlines() if line.startswith("SOURCE="))
        Path(output).write_text("damaged source")
        failing.unlink()
        self.assertRegex(self.cli("status"), r"Task sample\s+failed\s+1/6")
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":2, "right":2, "mid":2, "join":1})

    def test_retry_command_policy_does_not_reuse_an_obsolete_tracked_baseline(self):
        failing = self.work / "fail-right"
        failing.touch()
        trace = shlex.quote(str(self.work / "trace"))
        original = f"echo right >> {trace}; if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt"
        self.configure_workflow(right_command=original)
        self.configure(use_spec_hashes=True)
        self.cli("run")
        self.settle()
        failing.unlink()
        self.configure_workflow(right_command=f"echo right >> {trace}; printf repaired > same.txt")
        self.assertIn("Run", self.cli("explain"))
        self.configure(use_spec_hashes=False)
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftrepaired")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":2, "join":1})
        self.configure_workflow(right_command=original)
        self.configure(use_spec_hashes=True)
        self.assertIn("Run", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":2, "right":3, "join":2})

    def test_queued_dependents_block_rebinding_without_automatic_cancellation(self):
        from gwf.backends.local import Client, LocalStatus
        held, release, failing = (self.work / name for name in ("left-held", "left-release", "fail-right"))
        failing.touch()
        trace = shlex.quote(str(self.work / "trace"))
        self.configure_workflow(
            left_command=f"echo left >> {trace}; touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf left > same.txt",
            right_command=f"echo right >> {trace}; if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt",
        )
        env = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=env)
        self.wait_for(held.exists)
        def right_failed():
            with Client.connect(port=self.port) as client:
                return LocalStatus.FAILED in client.status().values()
        self.wait_for(right_failed)
        try:
            failing.unlink()
            self.assertIn("Active dependent work blocks retry: join", self.cli("-b", "slurm", "run", env=env, success=False))
            self.assertFalse((self.work / "cancellation-requested").exists())
            self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":1})
        finally:
            release.touch()
        self.settle()
        self.cli("-b", "slurm", "run", env=env)
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":2, "join":1})

    def test_running_dependent_keeps_its_input_generation_until_it_settles(self):
        held, release, failing = (self.work / name for name in ("consumer-held", "consumer-release", "fail-right"))
        failing.touch()
        trace = shlex.quote(str(self.work / "trace"))
        consumer = (f"echo consumer >> {trace}; printf '%s' {{source}} > {shlex.quote(str(self.work / 'source-path'))}; touch {shlex.quote(str(held))}; "
                    f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; cat {{source}} > {{out}}")
        (self.work / "workflow.py").write_text(
            TASK_FACTORY + "from gwflow import Task, Workflow, shell\ngwf = Workflow()\ntask = empty_task(inputs=[])\n"
            "left = task.target('left', inputs=[], outputs=['left.txt'])\n"
            f"left << {('echo left >> ' + trace + '; printf left > left.txt')!r}\n"
            "right = task.target('right', inputs=[], outputs=['right.txt'])\n"
            f"right << {('echo right >> ' + trace + '; if [ -e ' + shlex.quote(str(failing)) + ' ]; then exit 8; fi; touch right.txt')!r}\n"
            "consumer = task.target('consumer', inputs=[left.output('left.txt')], outputs=['out.txt'])\n"
            f"consumer << shell({consumer!r}, source=left.output('left.txt'), out=consumer.output('out.txt'))\n"
            "task.retain('result', source=consumer.output('out.txt'), path='result.txt')\n"
            "gwf.task(task, alias='sample')\n"
        )
        self.cli("run")
        self.wait_for(held.exists)
        try:
            source = (self.work / "source-path").read_text()
            Path(source).write_text("damaged source")
            failing.unlink()
            self.assertIn("Active dependent work blocks retry: consumer", self.cli("run", success=False))
            self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":1, "right":1, "consumer":1})
        finally:
            release.touch()
        self.settle()
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "left")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left":2, "right":2, "consumer":2})
