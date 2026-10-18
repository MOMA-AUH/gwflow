"""Task log discovery and individual streams through the installed CLI."""

import json
import re
import shlex
import shutil
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
from terminal_session import TerminalSession
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
        self.assertIn("Task sample: current attempt", output)
        self.assertNotRegex(output, r"[0-9a-f]{32}")
        self.assertNotIn("Backend job:", output)
        self.assertNotIn(str(self.work), output)
        self.assertLess(output.index("sample__right"), output.index("sample__left"))
        for name in ("right", "left", "join", "gwflow_prepare", "gwflow_complete"):
            self.assertIn(f"sample__{name}", output)
        self.assertIn("failed", output)
        self.assertIn("complete", output)
        self.assertIn("stdout: available", output)
        self.assertIn("stderr: available", output)
        self.assertIn("gwflow run sample --dry-run --details", output)
        self.assertNotIn("job-output", output)
        rows = [line for line in output.splitlines() if line.startswith("  sample__")]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all("stdout:" in line and "stderr:" in line for line in rows), output)
        self.assertIn("Public job names follow the current submission", output)
        self.assertIn("exact submission names keep the same log after a retry", output)
        public = [shlex.split(line.strip()) for line in output.splitlines()
                  if line.strip().startswith("gwflow logs sample__right ")]
        self.assertEqual(len(public), 2, output)
        self.assertIn("gwflow logs sample --details", output)
        details = self.cli("logs", "sample", "--details")
        self.assertRegex(details, r"Task sample.*attempt [0-9a-f]{32}")
        self.assertIn("Backend:", details)
        self.assertIn("Backend job:", details)
        commands = [shlex.split(line.strip()) for line in details.splitlines()
                    if line.strip().startswith("gwflow logs sample__right__")]
        self.assertEqual(len(commands), 2, details)
        for command in commands:
            stream_name = "stderr" if "--stderr" in command else "stdout"
            self.assertIn(str(self.work / ".gwf/logs" / (command[2] + "." + stream_name)), details)
            stream = self.cli(*command[1:])
            self.assertIn("job-error" if "--stderr" in command else "job-output", stream)
            public_command = next(item for item in public if ("--stderr" in item) == ("--stderr" in command))
            self.assertEqual(self.cli(*public_command[1:]), stream)
            self.assertNotIn("Task sample:", stream)
        self.assertEqual(self.snapshot(), before)

    def test_scheduler_outage_and_missing_streams_preserve_recorded_evidence(self):
        self.configure_workflow(right_command="echo readable-output; echo readable-error >&2; exit 8")
        self.cli("run")
        self.settle()
        index = self.cli("logs", "sample", "--details")
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
        output = self.cli("logs", "sample", "--details")
        self.assertIn("live state unavailable", output)
        self.assertIn(submission, output)
        self.assertEqual(self.snapshot(), before)

    def test_details_requires_task_index_and_keeps_owner_and_command_context(self):
        self.configure_workflow(left_command="echo exact-output; echo exact-error >&2; printf left > same.txt")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("alias='sample'", "alias='sample', key='two'")
                            .replace("task.target('left'", "task.target('left__part'"))
        self.run_complete()
        workflow.write_text(workflow.read_text() + "custom = gwf\n")
        workflow.rename(self.work / "custom flow.py")
        options = ("--file", "custom flow.py:custom", "--backend", "local")
        index = self.cli(*options, "logs", "sample__two", "--details")
        submission = re.search(r"sample__two__left__part: complete; submission (\S+)", index).group(1)
        self.cli(*options, "run", "sample__two", "--force")
        self.finish()
        for removed in (False, True):
            if removed:
                (self.work / "custom flow.py").write_text("from gwflow import Workflow\ncustom = Workflow()\n")
            before = self.snapshot()
            for selector in ("sample__two__left__part", submission):
                for stream in ((), ("--stderr",)):
                    with self.subTest(removed=removed, selector=selector, stream=stream):
                        result = self.cli_result(*options, "logs", selector, "--details", *stream,
                                                 "--no-pager", success=False)
                        self.assertEqual(result.returncode, 2)
                        self.assertEqual(result.stdout, "")
                        self.assertIn("--details requires a Task index", result.stderr)
                        self.assertIn(shlex.join(["gwflow", *options, "logs", "sample__two", "--details"]), result.stderr)
                        self.assertIn(shlex.join(["gwflow", *options, "logs", selector, *stream, "--no-pager"]), result.stderr)
                        raw = self.cli(*options, "logs", selector, *stream, "--no-pager")
                        self.assertIn("exact-error" if stream else "exact-output", raw)
                        self.assertNotIn("Task sample", raw)
            self.assertEqual(self.snapshot(), before)

    def test_narrow_terminal_and_redirected_indexes_preserve_complete_names_and_receipts(self):
        name = "long_" + "sample_" * 14 + "last"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("alias='sample'", f"alias={name!r}"))
        self.run_complete()
        first = self.cli("logs", name, "--details")
        submission = re.search(r"; submission (\S+)", first).group(1)
        self.cli("run", name, "--force")
        self.finish()
        for options in ((), ("--details",), ("--history",), ("--history", "--details")):
            with self.subTest(options=options):
                redirected = self.cli_result("logs", name, *options).stdout
                narrow = self.terminal_cli("logs", name, *options, width=40).stdout
                self.assertEqual(narrow, redirected)
                self.assertIn(name + "__gwflow_complete", narrow)
                self.assertNotIn("\x1b", narrow)
                self.assertNotIn("…", narrow)
                if "--history" in options:
                    self.assertIn(submission, narrow)
                if "--details" in options:
                    self.assertIn(str(self.work / ".gwf/logs"), narrow)

    def test_current_public_job_owner_wins_over_historical_name_overlap(self):
        self.configure_workflow(left_command="echo old-log; printf left > same.txt")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("alias='sample'", "alias='a__b'"))
        self.run_complete()
        old = self.cli("logs", "a__b", "--details")
        submission = re.search(r"a__b__left: complete; submission (\S+)", old).group(1)
        self.configure_workflow(left_command="echo new-log; printf left > same.txt")
        workflow.write_text(workflow.read_text().replace("alias='sample'", "alias='a'")
                            .replace("task.target('left'", "task.target('b__left'"))
        self.run_complete()
        for removed in (False, True):
            if removed:
                workflow.write_text("from gwflow import Workflow\ngwf = Workflow()\n")
            for selector, owner, content in (("a__b__left", "a", "new-log"), (submission, "a__b", "old-log")):
                with self.subTest(removed=removed, selector=selector):
                    error = self.cli("logs", selector, "--details", success=False)
                    self.assertIn(f"Use gwflow logs {owner} --details", error)
                    self.assertIn(content, self.cli("logs", selector, "--no-pager"))

    def test_combined_index_flags_keep_owner_and_raw_stream_guidance(self):
        self.run_complete()
        result = self.cli_result("logs", "sample__left", "--history", "--details", "--stderr",
                                 "--no-pager", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Use gwflow logs sample --history --details", result.stderr)
        self.assertIn("Read the stream with gwflow logs sample__left --stderr --no-pager", result.stderr)

    def test_individual_streams_keep_pager_and_no_pager_behavior(self):
        self.configure_workflow(left_command="echo pager-output; echo pager-error >&2; printf left > same.txt")
        self.run_complete()
        pager = self.work / "capture pager.py"
        pager.write_text("import sys\nfrom pathlib import Path\n"
                         "Path('paged-stream.txt').write_text(sys.stdin.read())\n"
                         "print('PAGER_FINISHED')\n")
        environment = {"PAGER": shlex.join([sys.executable, str(pager)])}
        capture = self.work / "paged-stream.txt"
        for stream in ((), ("--stderr",)):
            raw = self.cli_result("logs", "sample__left", *stream, "--no-pager").stdout
            for no_pager in (False, True):
                with self.subTest(stream=stream, no_pager=no_pager):
                    capture.unlink(missing_ok=True)
                    options = ("--no-pager",) if no_pager else ()
                    session = TerminalSession([self.executable, "logs", "sample__left", *stream, *options],
                                              cwd=self.work, env=environment)
                    try:
                        self.assertEqual(session.finish(), 0, session.text)
                        if no_pager:
                            self.assertFalse(capture.exists())
                            self.assertIn("pager-error" if stream else "pager-output", session.text)
                        else:
                            self.assertEqual(capture.read_text(), raw)
                            self.assertIn("PAGER_FINISHED", session.text)
                        self.assertNotIn("Task sample:", session.text)
                    finally:
                        session.close()

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

    def test_stalled_worker_connection_falls_back_to_recorded_logs(self):
        self.run_complete()
        before = self.snapshot()
        output = self.cli("-b", "recovery_fixture", "logs", "sample",
                          env=self.inject(stall_connection=True))
        self.assertIn("live state unavailable: scheduler observation timed out", output)
        self.assertIn("sample__left: complete", output)
        self.assertIn("stdout: available", output)
        self.assertEqual(self.snapshot(), before)

    def test_retained_public_and_historical_submission_names_cannot_ambiguously_select_a_task(self):
        self.configure_workflow(left_command="echo old-left-log; printf left > same.txt")
        self.run_complete()
        first = self.cli("logs", "sample", "--details")
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
                for options in ((), ("--history",)):
                    with self.subTest(ambiguous=ambiguous, selectors=selectors, options=options):
                        error = self.cli("logs", *selectors, *options, success=False)
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
        options = ("--file", "custom flow.py:custom", "--backend", "local")
        output = self.cli(*options, "logs", "sample")
        command = shlex.split(next(line.strip() for line in output.splitlines()
                                  if line.strip().startswith("gwflow ")))
        self.assertEqual(command[1:5], list(options[:4]))
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
        first = self.cli("logs", "sample", "--details")
        old_submission = re.search(r"gwflow logs (sample__compute__\S+)", first).group(1)
        self.cli("run", "sample", "--force")
        self.finish()
        before = self.snapshot()
        pulls = self.calls("pull")
        self.configure_workflow("docker://example.org/tools/unavailable:new")
        self.options(fail=True)
        index = self.cli("logs", "sample", "--details")
        submission = re.search(r"gwflow logs (sample__compute__\S+)", index).group(1)
        self.cli("logs", submission, "--no-pager")
        self.cli("logs", "sample__compute", "--stderr", "--no-pager")
        self.assertEqual(self.calls("pull"), pulls)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn(old_submission, index)
        history = self.cli("logs", "sample", "--history")
        self.assertIn(old_submission, history)
        self.cli("logs", old_submission, "--no-pager")
        self.assertEqual(self.calls("pull"), pulls)
        self.assertEqual(self.snapshot(), before)
        preview = self.cli("run", "sample", "--dry-run", "--details", success=False)
        self.assertIn("fixture registry unavailable", preview)


class TaskLogHistoryTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    @staticmethod
    def submissions(index):
        return re.findall(r"; submission (\S+)", index)

    def test_previous_attempt_without_submissions_remains_visible(self):
        interrupted = subprocess.run([
            sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "after_ready", "sample", "run",
        ], cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(interrupted.returncode, 93, interrupted.stdout + interrupted.stderr)
        current = self.cli("logs", "sample", "--details")
        previous = re.search(r"attempt ([0-9a-f]{32})", current).group(1)
        self.assertIn("submission not submitted", current)
        self.cli("run", "sample", "--force")
        self.finish()
        before = self.snapshot()
        self.assertNotIn(previous, self.cli("logs", "sample"))
        history = self.cli("logs", "sample", "--history")
        self.assertIn("Historical attempt " + previous, history)
        self.assertIn("No recorded submissions", history)
        self.assertEqual(self.snapshot(), before)

    def test_history_requires_task_index_mode(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        submission = re.search(r"gwflow logs (sample__left__\S+)", index).group(1)
        self.cli("run", "sample", "--force")
        self.finish()
        before = self.snapshot()
        for selector in ("sample__left", submission):
            with self.subTest(selector=selector):
                error = self.cli("logs", selector, "--history", "--no-pager", success=False)
                self.assertIn("--history requires a Task index", error)
                self.cli("logs", selector, "--no-pager")
        error = self.cli("logs", "sample", "--history", "--stderr", success=False)
        self.assertIn("--stderr requires one exact individual job", error)
        self.assertEqual(self.snapshot(), before)

    def test_history_selects_removed_and_unstarted_tasks_with_every_selector_validated(self):
        self.run_complete()
        first = self.cli("logs", "sample", "--details")
        previous = re.search(r"attempt ([0-9a-f]{32})", first).group(1)
        self.cli("run", "sample", "--force")
        self.finish()
        (self.work / "workflow.py").write_text(
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
                (("sam*", "batch__*", "sample"), ["batch__one", "batch__two", "sample"]),
                (("batch__one",), ["batch__one"])):
            with self.subTest(selectors=selectors):
                output = self.cli("logs", *selectors, "--history")
                self.assertEqual(re.findall(r"(?m)^Task (\S+):", output), expected)
                self.assertIn("Task batch__one: no recorded work", output)
                if "sample" in expected:
                    self.assertIn("no longer declared", output)
                    self.assertIn("Historical attempt " + previous, output)
                    self.assertIn("Restore", output)
                    self.assertNotIn("gwflow run sample", output)
                else:
                    self.assertNotIn(previous, output)
        error = self.cli("logs", "sample", "missing*", "batch", "--history", success=False)
        self.assertIn("Unmatched Task selectors", error)
        self.assertIn("'missing*'", error)
        self.assertIn("'batch'", error)
        self.assertNotIn("Task sample:", error)
        self.assertEqual(self.snapshot(), before)

    def test_history_marks_resolved_lifecycle_failures(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"crash_before_execution": True}))
        for local in ("gwflow_prepare", "gwflow_complete"):
            with self.subTest(local=local):
                environment = self.inject(job_fault=f"sample__{local}")
                self.cli("-b", "recovery_fixture", "run", "sample", "--force", env=environment)
                self.settle()
                failed = self.cli("logs", "sample", "--details")
                previous = re.search(r"attempt ([0-9a-f]{32})", failed).group(1)
                submission = re.search(rf"sample__{local}: failed; submission (\S+)", failed).group(1)
                self.cli("run", "sample", "--force")
                self.settle()
                self.assertIn("Reuse", self.cli("run", "--dry-run"))
                before = self.snapshot()
                current = self.cli("logs", "sample", "--details")
                self.assertNotIn(submission, current)
                self.assertNotIn(": failed", current)
                history = self.cli("logs", "sample", "--history")
                self.assertIn("Historical attempt " + previous, history)
                self.assertIn(f"sample__{local}: failed (historical); submission {submission}", history)
                command = next(shlex.split(line.strip()) for line in history.splitlines()
                               if line.strip().startswith("gwflow logs " + submission))
                self.cli(*command[1:])
                self.assertEqual(self.snapshot(), before)

    def test_expired_scheduler_history_and_missing_old_streams_remain_inspectable(self):
        self.configure_workflow(left_command="echo old-log; printf left > same.txt")
        self.run_complete()
        first = self.cli("logs", "sample", "--details")
        submission = re.search(r"gwflow logs (sample__left__\S+)", first).group(1)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("task.target('left'", "task.target('renamed'"))
        self.cli("run", "sample", "--force")
        self.finish()
        before = self.snapshot()
        environment = self.inject(fail_status_prefix="sample__left")
        current = self.cli("-b", "recovery_fixture", "logs", "sample", env=environment)
        self.assertNotIn("live state unavailable", current)
        history = self.cli("-b", "recovery_fixture", "logs", "sample", "--history", env=environment)
        self.assertIn("live state unavailable", history)
        self.assertIn(submission, history)
        command = next(shlex.split(line.strip()) for line in history.splitlines()
                       if "logs " + submission in line and "--stderr" not in line)
        self.assertIn("old-log", self.cli(*command[1:], env=environment))
        environment = self.inject(job_states={"sample__left": "UNKNOWN"})
        history = self.cli("-b", "recovery_fixture", "logs", "sample", "--history", env=environment)
        self.assertIn(f"sample__left: complete (historical); submission {submission}; live state unavailable", history)
        for stream in ("stderr", "stdout"):
            (self.work / ".gwf/logs" / (submission + "." + stream)).unlink()
            history = self.cli("logs", "sample", "--history")
            entry = next(section for section in history.split("    sample__") if submission in section)
            self.assertIn(stream + ": missing", entry)
        self.assertIn("No such file", self.cli("logs", submission, "--no-pager", success=False))
        self.assertEqual(self.snapshot(), before)

    def test_history_distinguishes_retry_submissions_and_previous_attempts_after_cleanup(self):
        failing = self.work / "fail-right"
        failing.touch()
        self.configure_workflow(right_command=(
            "echo right-log; echo right-error >&2; "
            f"if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt"))
        self.cli("run")
        self.settle()
        first = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", first).group(1)
        original = set(self.submissions(first))
        failed = re.search(r"sample__right: failed; submission (\S+)", first).group(1)
        failing.unlink()
        self.cli("run")
        self.settle()
        self.assertIn("Reuse", self.cli("run", "--dry-run"))
        current = self.cli("logs", "sample", "--details")
        retried = set(self.submissions(current))
        self.assertIn(attempt, current)
        self.assertNotIn(failed, current)
        before = self.snapshot()
        history = self.cli("logs", "sample", "--history", "--details")
        present, earlier = history.split("Historical submissions in current attempt " + attempt)
        self.assertEqual(set(self.submissions(present)), retried)
        self.assertEqual(set(self.submissions(earlier)), original - retried)
        self.assertIn("sample__right: failed (historical)", earlier)
        compact = self.cli("logs", "sample", "--history")
        compact_current, compact_earlier = compact.split("Historical submissions in current attempt " + attempt)
        self.assertNotIn("; submission ", compact_current)
        self.assertNotIn("Backend job:", compact)
        self.assertNotIn("Path:", compact)
        self.assertIn(failed, compact_earlier)
        self.assertIn("gwflow logs sample --history --details", compact)
        self.assertEqual(self.snapshot(), before)

        self.cli("run", "sample", "--force")
        self.settle()
        self.assertIn("Reuse", self.cli("run", "--dry-run"))
        latest = self.cli("logs", "sample", "--details")
        fresh = set(self.submissions(latest))
        self.assertNotIn(attempt, latest)
        self.assertTrue(fresh.isdisjoint(original | retried))
        self.cli("clean", "--attempt", attempt, "--delete")
        self.cli("clean", "sample", "--delete")
        before = self.snapshot()
        history = self.cli("logs", "sample", "--history", "--details")
        present, earlier = history.split("Historical attempt " + attempt)
        self.assertEqual(set(self.submissions(present)), fresh)
        self.assertCountEqual(self.submissions(earlier), original | retried)
        self.assertIn("sample__left: complete (historical)", earlier)
        self.assertIn("sample__gwflow_prepare: complete (historical)", earlier)
        self.assertIn("sample__gwflow_complete: complete (historical)", earlier)
        for line in history.splitlines():
            if line.strip().startswith("gwflow logs "):
                self.cli(*shlex.split(line.strip())[1:])
        self.assertIn("right-log", self.cli("logs", failed, "--no-pager"))
        self.assertIn("right-error", self.cli("logs", failed, "--stderr", "--no-pager"))
        self.assertEqual(self.snapshot(), before)
