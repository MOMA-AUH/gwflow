"""Task log discovery and individual streams through the installed CLI."""

import json
import re
import shlex
import shutil

from support import FIXTURES, LocalBackendTestCase
import test_graphs
import test_inspection
import test_registry_images


class TaskLogTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def test_current_index_orders_failures_and_provides_readable_stream_commands(self):
        self.configure_workflow(right_command="echo job-output; echo job-error >&2; exit 8")
        self.cli("run")
        self.settle()
        before = self.snapshot()
        output = self.cli("logs", "sample")
        self.assertRegex(output, r"Task sample.*attempt [0-9a-f]{32}")
        self.assertLess(output.index("sample__right"), output.index("sample__left"))
        for name in ("right", "left", "join", "gwflow_prepare", "gwflow_complete"):
            self.assertIn(f"sample__{name}", output)
        self.assertIn("failed", output)
        self.assertIn("complete", output)
        self.assertIn("stdout: available", output)
        self.assertIn("stderr: available", output)
        self.assertIn("gwflow run sample --dry-run --details", output)
        self.assertNotIn("job-output", output)
        commands = [shlex.split(line.strip()) for line in output.splitlines()
                    if line.strip().startswith("gwflow logs sample__right__")]
        self.assertEqual(len(commands), 2, output)
        for command in commands:
            stream = self.cli(*command[1:])
            self.assertIn("job-error" if "--stderr" in command else "job-output", stream)
        self.assertEqual(self.snapshot(), before)

    def test_scheduler_outage_and_missing_streams_preserve_recorded_evidence(self):
        self.configure_workflow(right_command="echo readable-output; echo readable-error >&2; exit 8")
        self.cli("run")
        self.settle()
        index = self.cli("logs", "sample")
        submission = re.search(r"gwflow logs (sample__right__\S+)", index).group(1)
        (self.work / ".gwf/logs" / (submission + ".stderr")).unlink()
        before = self.snapshot()
        environment = self.inject(fail_status_prefix="sample__left")
        output = self.cli("-b", "recovery_fixture", "logs", "sample", env=environment)
        self.assertIn("live state unavailable", output)
        self.assertIn("sample__right: failed", output)
        self.assertIn("stdout: available", output)
        self.assertIn("stderr: missing", output)
        self.assertIn("readable-output", self.cli("-b", "recovery_fixture", "logs", submission,
                                                  "--no-pager", env=environment))
        error = self.cli("logs", submission, "--stderr", "--no-pager", success=False)
        self.assertIn("No such file", error)
        self.assertNotIn("Traceback", error)
        (self.work / ".gwf/logs" / (submission + ".stdout")).unlink()
        self.assertIn("stdout: missing", self.cli("logs", "sample"))
        self.assertIn("No such file", self.cli("logs", submission, "--no-pager", success=False))
        # An unavailable backend connection also leaves disk evidence readable.
        self.configure(**{"backend.local.port": 0})
        output = self.cli("logs", "sample")
        self.assertIn("live state unavailable", output)
        self.assertIn(submission, output)
        self.assertEqual(self.snapshot(), before)

    def test_indexes_select_declared_and_removed_tasks_and_validate_every_selector(self):
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(
            "from gwflow import Task, Workflow, task_template\n"
            "@task_template\n"
            "def empty():\n"
            "    return Task(inputs=[])\n"
            "gwf = Workflow()\n"
            "gwf.task(empty(), alias='batch', key='one')\n"
            "gwf.task(empty(), alias='batch', key='two')\n")
        before = self.snapshot()
        for selectors, expected in (
                ((), ["batch__one", "batch__two", "sample"]),
                (("sample", "batch__one"), ["batch__one", "sample"]),
                (("batch__*", "batch__one"), ["batch__one", "batch__two"]),
                (("sample", "sample"), ["sample"])):
            with self.subTest(selectors=selectors):
                output = self.cli("logs", *selectors, "--no-pager")
                self.assertEqual(re.findall(r"(?m)^Task (\S+):", output), expected)
                if "sample" in expected:
                    self.assertIn("no longer declared", output)
                    self.assertIn("Restore", output)
                    self.assertNotIn("gwflow run sample", output)
                if "batch__one" in expected:
                    self.assertIn("Task batch__one: no recorded work", output)
        error = self.cli("logs", "sample", "absent*", "batch", "empty", "sample__left", success=False)
        self.assertIn("Unmatched Task selectors", error)
        for selector in ("absent*", "batch", "empty", "sample__left"):
            self.assertIn(repr(selector), error)
        self.assertNotIn("Task sample:", error)
        for selectors in ((), ("sample",), ("batch__*",)):
            error = self.cli("logs", *selectors, "--stderr", success=False)
            self.assertIn("individual job", error)
        self.assertEqual(self.snapshot(), before)

    def test_retained_public_and_historical_submission_names_cannot_ambiguously_select_a_task(self):
        self.configure_workflow(left_command="echo old-left-log; printf left > same.txt")
        self.run_complete()
        first = self.cli("logs", "sample")
        submission = re.search(r"gwflow logs (sample__left__\S+)", first).group(1)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("task.target('left'", "task.target('renamed'"))
        self.cli("run", "sample", "--force")
        self.finish()
        self.assertNotIn(submission, self.cli("logs", "sample"))
        self.assertIn("old-left-log", self.cli("logs", submission, "--no-pager"))
        self.cli("clean", "--delete")
        before = self.snapshot()
        for ambiguous in ("sample__left", submission):
            workflow.write_text(
                "from gwflow import Task, Workflow, task_template\n"
                "@task_template\n"
                "def empty():\n"
                "    return Task(inputs=[])\n"
                f"gwf = Workflow()\ngwf.task(empty(), alias={ambiguous!r})\n")
            for selectors in ((ambiguous,), ("*",), ()):
                with self.subTest(ambiguous=ambiguous, selectors=selectors):
                    error = self.cli("logs", *selectors, success=False)
                    self.assertIn("ambiguous", error.lower())
                    self.assertIn(ambiguous, error)
            self.cli("logs", "sample__right", "--no-pager")
        error = self.cli("logs", "unknown_task", success=False)
        self.assertIn("Unmatched Task selectors", error)
        self.assertEqual(self.snapshot(), before)

    def test_index_commands_preserve_explicit_workflow_and_backend_options(self):
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() + "custom = gwf\n")
        workflow.rename(self.work / "custom flow.py")
        options = ("--file", "custom flow.py:custom", "--backend", "local", "--no-color")
        output = self.cli(*options, "logs", "sample")
        command = shlex.split(next(line.strip() for line in output.splitlines()
                                  if line.strip().startswith("gwflow ")))
        self.assertEqual(command[1:6], list(options[:5]))
        self.cli(*command[1:])
        preview = shlex.split(next(line.split("Next action and reason: ")[1] for line in output.splitlines()
                                  if "Next action and reason:" in line))
        self.assertIn("Reuse", self.cli(*preview[1:]))

    def test_failed_preparation_and_completion_jobs_appear_first(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"crash_before_execution": True}))
        for local in ("gwflow_prepare", "gwflow_complete"):
            with self.subTest(local=local):
                environment = self.inject(job_fault=f"sample__{local}")
                self.cli("-b", "recovery_fixture", "run", "sample", "--force", env=environment)
                self.settle()
                before = self.snapshot()
                output = self.cli("logs", "sample")
                jobs = re.findall(r"(?m)^  (sample__\S+):", output)
                self.assertEqual(jobs[0], f"sample__{local}")
                self.assertIn(f"sample__{local}: failed", output)
                self.cli("logs", f"sample__{local}", "--no-pager")
                self.assertEqual(self.snapshot(), before)

    def test_available_live_states_distinguish_queued_and_running_jobs(self):
        self.run_complete()
        before = self.snapshot()
        environment = self.inject(job_states={"sample__left": "RUNNING", "sample__right": "SUBMITTED"})
        output = self.cli("-b", "recovery_fixture", "logs", "sample", env=environment)
        self.assertIn("sample__left: running", output)
        self.assertIn("sample__right: queued", output)
        self.assertEqual(self.snapshot(), before)


class LogImageInspectionTests(test_registry_images.RegistryTestCase):
    def test_logs_do_not_acquire_images_or_plan_a_future_run(self):
        self.run_complete()
        before = self.snapshot()
        pulls = self.calls("pull")
        self.configure_workflow("docker://example.org/tools/unavailable:new")
        self.options(fail=True)
        index = self.cli("logs", "sample")
        submission = re.search(r"gwflow logs (sample__compute__\S+)", index).group(1)
        self.cli("logs", submission, "--no-pager")
        self.cli("logs", "sample__compute", "--stderr", "--no-pager")
        self.assertEqual(self.calls("pull"), pulls)
        self.assertEqual(self.snapshot(), before)
        preview = self.cli("run", "sample", "--dry-run", "--details", success=False)
        self.assertIn("fixture registry unavailable", preview)
