"""Lifecycle explanations at the installed CLI, filesystem, and backend seams."""

from collections import Counter
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
        explanation = self.cli(*backend, "explain", *options, env=env)
        dry_run = self.cli(*backend, "run", "--dry-run", *options, success=success, env=env)
        self.assertEqual(self.snapshot(), before)
        for line in re.findall(r"Task [^\n]+", explanation):
            self.assertIn(line, dry_run)
        self.assertEqual(re.findall(r"Would submit (\S+)", dry_run), re.findall(r"Would submit (\S+)", explanation))
        return explanation

    def run_previewed(self, preview, *options, success=True, env=None, backend=()):
        actual = self.cli(*backend, "run", *options, success=success, env=env)
        for line in re.findall(r"Task [^\n]+", preview):
            self.assertIn(line, actual)
        pending = re.findall(r"Would submit (\S+)", preview)
        self.assertEqual(actual.count("Submitted target "), len(pending))
        for public in pending:
            self.assertIn("Submitted target " + public, actual)
        return actual

    def test_fresh_reuse_and_selected_force_previews_match_actual_runs(self):
        preview = self.preview()
        self.assertIn("Task a: fresh;", preview)
        self.assertFalse((self.work / "work").exists())
        self.run_previewed(preview)
        self.finish()
        preview = self.preview()
        for name in ("a", "b", "c"):
            self.assertIn(f"Task {name}: reuse;", preview)
        self.run_previewed(preview)
        preview = self.preview("--force-task", "a")
        self.assertIn("Task a: fresh;", preview)
        self.assertIn("Task b: reuse;", preview)
        self.assertIn("Task c: fresh;", preview)
        self.assertIn("remove previous results", preview)
        self.run_previewed(preview, "--force-task", "a")
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})
        preview = self.preview("--force")
        for name in ("a", "b", "c"):
            self.assertIn(f"Task {name}: fresh; requested --force", preview)
        self.run_previewed(preview, "--force")
        self.finish()

    def test_detail_mapping_and_old_logs_survive_retry_force_and_cleanup(self):
        failing = self.work / "fail-right"
        failing.touch()
        test_graphs.TaskGraphTests.configure_workflow(self, right_command=(
            f"echo right >> {shlex.quote(str(self.work / 'trace'))}; echo LOCATION=$PWD; "
            f"if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt"
        ))
        self.cli("run")
        self.settle()
        before = self.snapshot()
        details = self.cli("status", "--details")
        first = re.search(r"sample__right: (\S+)", details).group(1)
        self.assertIn("(failed)", details)
        self.assertIn("Backend job:", details)
        self.assertIn(str(self.work / ".gwf/logs" / (first + ".stdout")), details)
        self.assertIn("Staging:", details)
        self.assertEqual(self.snapshot(), before)
        original_log = self.cli("logs", first, "--no-pager")
        failing.unlink()
        preview = self.preview()
        self.assertIn("Task sample: retry;", preview)
        self.run_previewed(preview)
        self.settle()
        self.cli("run", "--force")
        self.settle()
        details = self.cli("explain", "--details")
        latest = re.search(r"sample__right: (\S+)", details).group(1)
        self.assertNotEqual(first, latest)
        latest_log = self.cli("logs", latest, "--no-pager")
        self.assertEqual(self.cli("logs", "sample__right", "--no-pager"), latest_log)
        self.cli("clean-work", "--delete")
        self.assertEqual(self.cli("logs", first, "--no-pager"), original_log)
        self.assertEqual(self.cli("logs", "sample__right", "--no-pager"), latest_log)

    def test_compact_status_distinguishes_condition_from_next_action_and_cleaned_work(self):
        self.assertIn("Task a: incomplete computation; next: fresh", self.cli("status"))
        self.run_complete()
        self.assertIn("Task a: reusable work-present; next: reuse", self.cli("status"))
        (self.work / "results/a/result.txt").unlink()
        self.assertIn("Task a: repair-needed; next: repair", self.cli("status"))
        self.cli("run")
        self.finish()
        self.cli("clean-work", "--delete", "--task", "a")
        before = self.snapshot()
        self.assertIn("Task a: reusable work-cleaned; next: reuse", self.cli("status"))
        self.assertEqual(self.snapshot(), before)

    def test_changed_structure_and_commands_have_specific_reasons(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        workflow.write_text(original.replace("a = gwf.task_from_template",
                                             "task.retain('extra', source=target.output('private.txt'), path='extra.txt')\na = gwf.task_from_template"))
        self.assertIn("changed declared structure: retained mappings", self.preview())
        workflow.write_text(original.replace("printf a", "printf UPDATED_A"))
        preview = self.preview()
        self.assertIn("changed commands: compute", preview)
        self.assertIn("producer a will start a fresh attempt", preview)
        self.run_previewed(preview)
        self.finish()
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "UPDATED_A")

    def test_changed_input_metadata_reports_the_path_and_changed_fields(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("Task(inputs=[])", "Task(inputs=['input.txt'])", 1))
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
        self.assertIn("Task a: repair;", preview)
        self.assertIn("Task c: deferred;", preview)
        self.assertIn("later invocation", preview)
        self.run_previewed(preview)
        self.finish()
        preview = self.preview()
        self.assertIn("Task c: reuse;", preview)
        self.run_previewed(preview)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 1, "c": 1})

    def test_confirmed_rejection_previews_continuation_instead_of_fresh_allocation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        preview = self.preview()
        self.assertIn("Task a: continue;", preview)
        self.run_previewed(preview)
        self.finish()

    def test_uncertainty_previews_zero_submissions_for_the_whole_workflow(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="a__gwflow_prepare"), success=False)
        preview = self.preview(success=False)
        self.assertIn("Task a: blocked; unresolved submission", preview)
        self.assertNotIn("Would submit", preview)
        self.run_previewed(preview, success=False)
        self.assertFalse((self.work / "trace").exists())
        self.assertIn("Task a: blocked activity", self.cli("status"))

    def test_active_work_previews_no_duplicate_admission_and_force_reports_conflicts(self):
        held, release = self.work / "held", self.work / "release"
        wait = f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("echo a", wait + "echo a"))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            self.wait_for(lambda: "Task b: reuse;" in self.cli("explain", "b"))
            preview = self.preview()
            self.assertIn("Task a: active;", preview)
            self.run_previewed(preview)
            preview = self.preview("--force-task", "a", success=False)
            self.assertIn("active work blocks replacement", preview)
            self.run_previewed(preview, "--force-task", "a", success=False)
            self.assertIn("Task a: incomplete computation (active)", self.cli("status"))
        finally:
            release.touch()
        self.finish()

    def test_display_filters_preserve_whole_workflow_validation_and_force_planning(self):
        self.run_complete()
        before = self.snapshot()
        filtered = self.cli("explain", "--force-task", "a", "c")
        self.assertIn("Task c: fresh;", filtered)
        self.assertIn("producer a will start a fresh attempt", filtered)
        self.assertNotIn("Task a:", filtered)
        self.assertEqual(self.snapshot(), before)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("b = gwf.task_from_template",
                                                        "target.outputs.append('../escape')\nb = gwf.task_from_template"))
        for command in (("explain", "a"), ("status", "a"), ("run", "--dry-run")):
            with self.subTest(command=command):
                self.assertIn("Invalid managed relative path", self.cli(*command, success=False))
                self.assertEqual(self.snapshot(), before)

    def test_changed_inputs_after_cleanup_are_still_explained(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("Task(inputs=[])", "Task(inputs=['input.txt'])", 1))
        self.run_complete()
        self.cli("clean-work", "--delete", "--task", "a")
        (self.work / "input.txt").write_text("changed after cleanup\n")
        preview = self.preview()
        self.assertIn("input metadata changed", preview)
        self.assertIn(str(self.work / "input.txt"), preview)
        self.run_previewed(preview)
        self.finish()

    def test_all_inspectors_wait_for_tracking_then_leave_evidence_unchanged(self):
        submitter, submitted = self.launch("-b", "recovery_fixture", "run",
                                          env=self.inject(hold_tracking=True), filename="submitted")
        inspectors = []
        try:
            self.wait_for((self.work / "tracking-held").exists)
            self.finish()
            before = self.snapshot()
            for index, command in enumerate((("status",), ("explain",), ("run", "--dry-run"), ("clean-work",))):
                process, output = self.launch(*command, filename=f"inspection-{index}")
                inspectors.append((process, output))
                self.wait_for(lambda: "Waiting for frontend" in output.read_text() or process.poll() is not None)
                self.assertIsNone(process.poll(), output.read_text())
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
            "run", "--force-task", "a",
        ], cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        preview = self.preview()
        self.assertIn("Task a: initialize;", preview)
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
        self.cli("clean-work", "--delete")
        missing.unlink()
        preview = self.preview()
        self.assertIn("Task a: fresh;", preview)
        self.assertIn("missing retained output: " + str(missing), preview)
        self.run_previewed(preview)
        self.finish()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])

    def test_interrupted_transfer_previews_only_finishing_and_preserves_computation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        preview = self.preview()
        self.assertIn("Task a: transfer;", preview)
        self.assertIn("Task a: results transfer; next: transfer", self.cli("status"))
        self.run_previewed(preview)
        self.settle()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_active_transfer_has_a_distinct_condition_and_no_new_submissions(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_manifest=True))
        self.wait_for((self.work / "manifest-held").exists)
        try:
            preview = self.preview()
            self.assertIn("Task a: active;", preview)
            self.assertIn("Task a: results transfer (active); next: active", self.cli("status"))
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

    def test_preparation_retry_preview_matches_actual_submission(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_before_baseline=True))
        self.settle()
        preview = self.preview()
        self.assertIn("Task sample: prepare;", preview)
        self.run_previewed(preview)
        self.settle()
        self.assertIn("Task sample: reuse;", self.preview())
