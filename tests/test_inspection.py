"""Lifecycle explanations at the installed CLI, filesystem, and backend seams."""

from collections import Counter
import json
import re
import os
import shlex
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_fresh
import test_graphs
import test_transfer
import test_inputs
import test_managed_recovery


class LifecycleInspectionTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    settle = test_fresh.FreshAttemptTests.settle
    inject = test_fresh.FreshAttemptTests.inject
    launch = test_managed_recovery.ManagedCoordinationTests.launch

    def snapshot(self):
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for root in (self.work / ".gwf/gwflow", self.work / "work", self.work / "results")
                for path in root.rglob("*") if path.is_file()}

    def preview(self, *options, success=True, env=None, backend=()):
        before = self.snapshot()
        dry_run = self.cli(*backend, "run", "--dry-run", "--details", *options, success=success, env=env)
        self.assertEqual(self.snapshot(), before)
        return dry_run

    def run_previewed(self, preview, *options, success=True, env=None, backend=()):
        actual = self.cli(*backend, "run", "--details", *options, success=success, env=env)
        for line in re.findall(r"Task [^\n]+", preview):
            self.assertIn(line, actual)
        pending = re.findall(r"(?m)^  Would submit (\S+)", preview)
        self.assertEqual(actual.count(": confirmed submitted"), len(pending))
        for public in pending:
            self.assertIn(public + ": confirmed submitted", actual)
        return actual

    def test_fresh_reuse_and_selected_force_previews_match_actual_runs(self):
        preview = self.preview()
        self.assertRegex(preview, r"Task a\s+Run\s+")
        self.assertFalse((self.work / "work").exists())
        self.run_previewed(preview)
        self.finish()
        preview = self.preview()
        for name in ("a", "b", "c"):
            self.assertRegex(preview, rf"Task {name}\s+Reuse\s+")
        self.run_previewed(preview)
        preview = self.preview("--force", "a")
        self.assertRegex(preview, r"Task a\s+Run\s+")
        self.assertNotRegex(preview, r"Task [bc]\s+")
        self.assertIn("remove previous results", preview)
        self.run_previewed(preview, "--force", "a")
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 1})
        preview = self.preview()
        self.assertRegex(preview, r"Task c\s+Run\s+")
        self.run_previewed(preview)
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})
        preview = self.preview("--force")
        for name in ("a", "b", "c"):
            self.assertRegex(preview, rf"Task {name}\s+Run\s+requested --force")
        self.run_previewed(preview, "--force")
        self.finish()

    def test_detail_mapping_and_old_logs_survive_retry_force_and_cleanup(self):
        failing = self.work / "fail-right"
        failing.touch()
        test_graphs.TaskGraphTests.configure_workflow(self, right_command=(
            f"echo right >> {shlex.quote(str(self.work / 'trace'))}; echo LOCATION=$PWD; echo job-error >&2; "
            f"if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt"
        ))
        self.cli("run")
        self.settle()
        before = self.snapshot()
        details = self.cli("run", "--dry-run", "--details")
        first = re.search(r"sample__right: (\S+)", details).group(1)
        first_attempt = re.search(r"Attempt: ([0-9a-f]{32})", details).group(1)
        self.assertIn("(failed)", details)
        self.assertIn("Backend job:", details)
        self.assertIn(str(self.work / ".gwf/logs" / (first + ".stdout")), details)
        self.assertIn("Staging:", details)
        self.assertEqual(self.snapshot(), before)
        original_log = self.cli("logs", first, "--no-pager")
        self.assertIn("job-error", self.cli("logs", first, "--stderr", "--no-pager"))
        self.assertNotIn("job-error", original_log)
        failing.unlink()
        preview = self.preview()
        self.assertRegex(preview, r"Task sample\s+Retry\s+")
        self.run_previewed(preview)
        self.settle()
        index = self.cli("logs", "sample", "--details")
        self.assertIn(first_attempt, index)
        self.assertNotIn(first, index)
        self.cli("run", "--force")
        self.settle()
        details = self.cli("run", "--dry-run", "--details")
        latest = re.search(r"sample__right: (\S+)", details).group(1)
        self.assertNotEqual(first, latest)
        latest_log = self.cli("logs", latest, "--no-pager")
        self.assertEqual(self.cli("logs", "sample__right", "--no-pager"), latest_log)
        self.cli("clean", "--delete")
        self.assertEqual(self.cli("logs", first, "--no-pager"), original_log)
        self.assertIn("job-error", self.cli("logs", first, "--stderr", "--no-pager"))
        for selector in ("sample", "sam*"):
            index = self.cli("logs", selector, "--no-pager", "--details")
            self.assertIn(latest, index)
            self.assertNotIn(first, index)
        self.cli("logs", "sample__*", "--no-pager", success=False)
        self.assertEqual(self.cli("logs", "sample__right", "--no-pager"), latest_log)

    def test_compact_status_distinguishes_condition_from_next_action_and_cleaned_work(self):
        self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+pending\s+0/3")
        self.assertIn("Next action: Run", self.cli("run", "--dry-run", "--details"))
        self.run_complete()
        self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+reusable\s+3/3\s+work present")
        (self.work / "results/a/result.txt").unlink()
        self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+repairable\s+2/3\s+retained results need repair")
        self.assertIn("Next action: Repair", self.cli("run", "--dry-run", "--details"))
        self.cli("run")
        self.finish()
        self.cli("clean", "--delete", "a")
        before = self.snapshot()
        self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+reusable\s+3/3\s+work cleaned")
        self.assertEqual(self.snapshot(), before)

    def test_changed_structure_and_commands_have_specific_reasons(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        workflow.write_text(original.replace("a = gwf.task",
                                             "task.retain('extra', source=target.output('private.txt'), path='extra.txt')\na = gwf.task"))
        self.assertIn("Reason: Changed declared structure: retained mappings", self.preview())
        workflow.write_text(original.replace("printf a", "printf UPDATED_A"))
        preview = self.preview()
        self.assertIn("Reason: Changed commands: compute", preview)
        self.assertIn("producer a will start a fresh attempt", preview)
        self.run_previewed(preview)
        self.finish()
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "UPDATED_A")

    def test_changed_input_metadata_reports_the_path_and_changed_fields(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("empty_task(inputs=[])", "empty_task(inputs=['input.txt'])", 1))
        self.run_complete()
        source = self.work / "input.txt"
        before = source.stat()
        source.write_text("changed input size\n")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns - 1000))
        preview = self.preview()
        self.assertIn("input metadata changed", preview)
        self.assertIn(str(source), preview)
        self.assertIn("size", preview)
        self.assertIn("mtime", preview)
        self.run_previewed(preview)
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_repair_defers_existing_consumer_and_previews_exact_submissions(self):
        self.run_complete()
        (self.work / "results/a/result.txt").unlink()
        preview = self.preview()
        self.assertRegex(preview, r"Task a\s+Repair\s+")
        self.assertRegex(preview, r"Task c\s+Defer\s+")
        self.assertIn("later invocation", preview)
        self.run_previewed(preview)
        self.finish()
        preview = self.preview()
        self.assertRegex(preview, r"Task c\s+Reuse\s+")
        self.run_previewed(preview)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 1, "c": 1})

    def test_confirmed_rejection_previews_continuation_instead_of_fresh_allocation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        preview = self.preview()
        self.assertRegex(preview, r"Task a\s+Continue\s+")
        self.run_previewed(preview)
        self.finish()

    def test_uncertainty_previews_zero_submissions_for_the_whole_workflow(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="a__gwflow_prepare"), success=False)
        preview = self.preview(success=False)
        self.assertRegex(preview, r"Task a\s+Blocked\s+submission outcome unknown")
        self.assertNotIn("Would submit", preview)
        self.run_previewed(preview, success=False)
        self.assertFalse((self.work / "trace").exists())
        self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+blocked")

    def test_active_work_previews_no_duplicate_admission_and_force_reports_conflicts(self):
        held, release = self.work / "held", self.work / "release"
        wait = f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("echo a", wait + "echo a"))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            self.wait_for(lambda: re.search(r"Task b\s+Reuse\s+", self.cli("run", "--dry-run")))
            preview = self.preview()
            self.assertRegex(preview, r"Task a\s+Wait\s+")
            self.run_previewed(preview)
            preview = self.preview("--force", "a", success=False)
            self.assertIn("active work blocks replacement", preview)
            self.run_previewed(preview, "--force", "a", success=False)
            self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+running")
        finally:
            release.touch()
        self.finish()

    def test_selected_preview_preserves_global_authoring_validation(self):
        self.run_complete()
        before = self.snapshot()
        filtered = self.cli("run", "--dry-run", "--force", "a")
        self.assertNotRegex(filtered, r"Task [bc]\s+")
        self.assertRegex(filtered, r"Task a\s+Run\s+")
        self.assertEqual(self.snapshot(), before)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("b = gwf.task",
                                                        "target.outputs.append('../escape')\nb = gwf.task"))
        for command in (("run", "a", "--dry-run"), ("status", "a"), ("run", "a")):
            with self.subTest(command=command):
                self.assertIn("Invalid managed relative path", self.cli(*command, success=False))
                self.assertEqual(self.snapshot(), before)

    def test_changed_inputs_after_cleanup_are_still_explained(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("empty_task(inputs=[])", "empty_task(inputs=['input.txt'])", 1))
        self.run_complete()
        self.cli("clean", "--delete", "a")
        (self.work / "input.txt").write_text("changed after cleanup\n")
        preview = self.preview()
        self.assertIn("input metadata changed", preview)
        self.assertIn(str(self.work / "input.txt"), preview)
        self.run_previewed(preview)
        self.finish()

    def test_all_inspectors_wait_for_tracking_then_leave_evidence_unchanged(self):
        commands = (("status",), ("run", "--dry-run"), ("clean",),
                    ("logs",), ("logs", "--history"), ("logs", "a__compute", "--no-pager"),
                    ("cancel", "a", "--dry-run"), ("cancel", "a"))
        # Each CLI gets its own startup allowance; the held frontend must
        # outlive all of them, including slow imports on shared filesystems.
        submitter, submitted = self.launch("-b", "recovery_fixture", "run",
                                          env=self.inject(hold_tracking=True, tracking_timeout=20 * (len(commands) + 1)),
                                          filename="submitted")
        inspectors = []
        try:
            self.wait_for((self.work / "tracking-held").exists)
            self.finish()
            before = self.snapshot()
            for index, command in enumerate(commands):
                process, output = self.launch(*command, filename=f"inspection-{index}")
                inspectors.append((process, output))
                self.wait_for(lambda: "Waiting for frontend" in output.read_text() or process.poll() is not None)
                self.assertIsNone(process.poll(), output.read_text() + "\nSubmitting frontend:\n" + submitted.read_text())
                self.assertNotIn("Task a:", output.read_text())
        finally:
            (self.work / "tracking-release").touch()
        submitter.wait(timeout=10)
        self.assertEqual(submitter.returncode, 0, submitted.read_text())
        for process, output in inspectors:
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0, output.read_text())
        self.assertEqual(self.snapshot(), before)

    def test_interrupted_initialization_preview_matches_actual_resume(self):
        self.run_complete()
        result = subprocess.run([
            sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "before_removal", "a",
            "run", "--force", "a",
        ], cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        preview = self.preview()
        self.assertRegex(preview, r"Task a\s+Run\s+")
        self.assertIn("remove previous results", preview)
        self.run_previewed(preview)
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})


class FinishingInspectionTests(LocalBackendTestCase):
    configure_workflow = test_transfer.TransferRecoveryTests.configure_workflow
    settle = test_transfer.TransferRecoveryTests.settle
    inject = test_transfer.TransferRecoveryTests.inject
    fault = test_transfer.TransferRecoveryTests.fault
    snapshot = LifecycleInspectionTests.snapshot
    preview = LifecycleInspectionTests.preview
    run_previewed = LifecycleInspectionTests.run_previewed

    def test_repair_progress_stays_reopened_through_queued_and_running_completion(self):
        self.run_complete()
        result = self.work / "results/samples/a/report/renamed.txt"
        expected = result.read_bytes()
        result.unlink()
        before = self.snapshot()
        for command in (("status",), ("run", "--dry-run")):
            output = self.cli_result(*command, "--details").stdout
            if command[0] == "status":
                self.assertRegex(output, r"(?m)^a\s+repairable\s+2/3")
            else:
                self.assertIn("State: repairable; Jobs completed: 2/3", output)
                self.assertIn("Next action: Repair", output)
            self.assertEqual(self.snapshot(), before)
        actual = self.cli_result("-b", "recovery_fixture", "run", "--details",
                                 env=self.fault(gate_after_manifest=True)).stdout
        self.assertIn("State: repairable; Jobs completed: 2/3", actual)
        self.assertIn("Submitted 1 job across 1 Task.", actual)
        self.wait_for((self.work / "manifest-held").exists)
        try:
            for state, backend, environment in (
                ("queued", ("-b", "recovery_fixture"), self.inject(queued_prefix="a__gwflow_complete")),
                ("finishing", (), None),
            ):
                before = self.snapshot()
                for command in (("status",), ("run", "--dry-run"), ("run",)):
                    with self.subTest(state=state, command=command):
                        output = self.cli_result(*backend, *command, "--details", env=environment).stdout
                        if command[0] == "status":
                            self.assertRegex(output, rf"(?m)^a\s+{state}\s+2/3")
                        else:
                            self.assertIn(f"State: {state}; Jobs completed: 2/3", output)
                            self.assertIn("Next action: Wait", output)
                        if command == ("run",):
                            self.assertIn("No new jobs were submitted.", output)
                        self.assertEqual(self.snapshot(), before)
        finally:
            (self.work / "manifest-release").touch()
        self.finish()
        for command in (("status",), ("run", "--dry-run"), ("run",)):
            output = self.cli_result(*command, "--details").stdout
            if command[0] == "status":
                self.assertRegex(output, r"(?m)^a\s+reusable\s+3/3")
            else:
                self.assertIn("State: reusable; Jobs completed: 3/3", output)
                self.assertIn("Next action: Reuse", output)
        self.assertEqual(result.read_bytes(), expected)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_repair_and_fresh_fallback_identify_missing_and_changed_retained_files(self):
        self.run_complete()
        root = self.work / "results/samples/a/report"
        missing, changed = root / "renamed.txt", root / "nested/two.txt"
        missing.unlink()
        before = changed.stat()
        changed.write_text("edited result")
        os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns + 1000))
        preview = self.preview()
        self.assertIn("missing retained output: " + str(missing), preview)
        self.assertIn(str(changed), preview)
        self.assertIn("size", preview)
        self.assertIn("mtime", preview)
        self.run_previewed(preview)
        self.finish()
        self.cli("clean", "--delete")
        missing.unlink()
        preview = self.preview()
        self.assertRegex(preview, r"Task a\s+Run\s+")
        self.assertIn("missing retained output: " + str(missing), preview)
        self.run_previewed(preview)
        self.finish()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])

    def test_interrupted_transfer_previews_only_finishing_and_preserves_computation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        preview = self.preview()
        self.assertRegex(preview, r"Task a\s+Finish\s+")
        self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+failed\s+2/3\s+results transfer needs recovery")
        self.assertIn("Next action: Finish", self.cli("run", "--dry-run", "--details"))
        self.run_previewed(preview)
        self.settle()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_active_transfer_has_a_distinct_condition_and_no_new_submissions(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_manifest=True))
        self.wait_for((self.work / "manifest-held").exists)
        try:
            preview = self.preview()
            self.assertRegex(preview, r"Task a\s+Wait\s+")
            self.assertRegex(self.cli("status", "*"), r"(?m)^a\s+finishing\s+2/3")
            self.assertNotIn("reusable", self.cli("status", "*"))
            self.run_previewed(preview)
        finally:
            (self.work / "manifest-release").touch()
        self.finish()


class PreparationInspectionTests(LocalBackendTestCase):
    configure_workflow = test_inputs.ExternalInputTests.configure_workflow
    settle = test_inputs.ExternalInputTests.settle
    inject = test_inputs.ExternalInputTests.inject
    fault = test_inputs.ExternalInputTests.fault
    snapshot = LifecycleInspectionTests.snapshot
    preview = LifecycleInspectionTests.preview
    run_previewed = LifecycleInspectionTests.run_previewed

    def test_continue_preserves_running_preparation_and_submits_only_remaining_jobs(self):
        self.fault(gate_before_preparation=True)
        environment = self.inject(job_fault="sample__gwflow_prepare", record_acceptances=True)
        submitter = subprocess.run([
            sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "after_preparation_ack", "sample",
            "-b", "recovery_fixture", "run",
        ], cwd=self.work, capture_output=True, text=True, env=environment, timeout=30)
        try:
            self.assertEqual(submitter.returncode, 110, submitter.stdout + submitter.stderr)
            self.wait_for((self.work / "preparation-held").exists)
            before = self.snapshot()
            for command in (("status",), ("run", "--dry-run")):
                output = self.cli_result("-b", "recovery_fixture", *command, "--details", env=environment).stdout
                if command[0] == "status":
                    self.assertRegex(output, r"(?m)^sample\s+preparing\s+0/3")
                else:
                    self.assertIn("State: preparing; Jobs completed: 0/3", output)
                    self.assertIn("Next action: Continue", output)
                self.assertRegex(output, r"\[preparation\]\s+running")
                self.assertEqual(self.snapshot(), before)
            output = self.cli_result("-b", "recovery_fixture", "run", "--details", env=environment).stdout
            self.assertIn("State: preparing; Jobs completed: 0/3", output)
            self.assertIn("Next action: Continue", output)
            self.assertIn("Submitted 2 jobs across 1 Task.", output)
        finally:
            (self.work / "preparation-release").touch()
        self.finish()
        accepted = [json.loads(line)["name"] for line in (self.work / "backend-acceptances.jsonl").read_text().splitlines()]
        self.assertEqual(len(accepted), 3)
        for local in ("gwflow_prepare", "read", "gwflow_complete"):
            self.assertEqual(sum(name.startswith(f"sample__{local}__") for name in accepted), 1)
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "hello\n")

    def test_preparation_retry_preview_matches_actual_submission(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_before_baseline=True))
        self.settle()
        preview = self.preview()
        self.assertRegex(preview, r"Task sample\s+Retry\s+")
        self.run_previewed(preview)
        self.settle()
        self.assertRegex(self.preview(), r"Task sample\s+Reuse\s+")
