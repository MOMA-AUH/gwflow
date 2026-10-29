"""Stable Next action reasons observed through the installed status command."""

import json
import shutil
import subprocess
import sys

from record_fixtures import read_record, record_path, remove_record, write_record
from support import FIXTURES, LocalBackendTestCase
import test_fresh
import test_graphs
import test_inspection


class JsonReasonTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    snapshot = test_inspection.LifecycleInspectionTests.snapshot
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle

    def report(self, *selectors, env=None):
        before = self.snapshot()
        backend = ("-b", "recovery_fixture") if env is not None else ()
        result = self.cli_result(*backend, "status", "--json", *selectors, env=env)
        self.assertEqual(self.snapshot(), before)
        return {row["name"]: row for row in json.loads(result.stdout)["tasks"]}

    def assertReason(self, task, action, code):
        self.assertEqual(task["next_action"], action)
        self.assertEqual(task["reason"]["code"], code)

    def test_changed_definition_and_upstream_keep_recorded_attempts(self):
        self.run_complete()
        before = self.report()
        for row in before.values():
            self.assertReason(row, "reuse", "results_valid")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a", "printf UPDATED_A"))
        after = self.report()
        self.assertReason(after["a"], "run", "definition_requires_refresh")
        self.assertReason(after["c"], "run", "upstream_attempt_changed")
        self.assertReason(after["b"], "reuse", "results_valid")
        for name in before:
            self.assertEqual(after[name]["attempt_id"], before[name]["attempt_id"])
        self.assertEqual(after["a"]["progress"], {"completed": 0, "total": 3})

    def test_input_changes_and_unavailable_input_keep_the_recorded_attempt(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("empty_task(inputs=[])", "empty_task(inputs=['input.txt'])", 1))
        self.run_complete()
        before = self.report("a")["a"]
        (self.work / "input.txt").write_text("changed input\n")
        row = self.report("a")["a"]
        self.assertReason(row, "run", "inputs_changed")
        self.assertEqual(row["attempt_id"], before["attempt_id"])
        (self.work / "input.txt").unlink()
        row = self.report("a")["a"]
        self.assertReason(row, "blocked", "input_unavailable")
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["progress"], {"completed": 3, "total": 3})

    def test_repair_deferral_and_lost_prior_work(self):
        self.run_complete()
        before = self.report()
        (self.work / "results/a/result.txt").unlink()
        rows = self.report()
        self.assertReason(rows["a"], "repair", "results_need_repair")
        self.assertReason(rows["c"], "defer", "upstream_recovery_pending")
        self.assertEqual(rows["a"]["state"], "repairable")
        self.assertEqual(rows["c"]["state"], "deferred")
        self.assertEqual(rows["c"]["progress"], {"completed": 3, "total": 3})
        self.assertEqual(rows["a"]["attempt_id"], before["a"]["attempt_id"])
        shutil.rmtree(self.work / "work/a")
        row = self.report("a")["a"]
        self.assertReason(row, "run", "prior_work_unusable")
        self.assertEqual(row["attempt_id"], before["a"]["attempt_id"])

    def test_interrupted_initialization_resumes_selected_identity(self):
        self.run_complete()
        result = subprocess.run(
            [sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work),
             "before_removal", "a", "run", "--force", "a"],
            cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        current = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/a/current.json"))
        identity = json.loads(current.read_text())["attempt"]
        row = self.report("a")["a"]
        self.assertReason(row, "run", "initialization_incomplete")
        self.assertEqual(row["attempt_id"], identity)
        self.assertEqual(row["progress"], {"completed": 0, "total": 3})

    def test_rejected_admission_continues_without_new_attempt(self):
        self.cli("-b", "recovery_fixture", "run", "a", env=self.inject(reject_before_admission=True), success=False)
        row = self.report("a")["a"]
        self.assertReason(row, "continue", "submissions_pending")
        self.assertIsInstance(row["attempt_id"], str)
        self.assertEqual(row["progress"], {"completed": 0, "total": 3})

    def test_failed_computation_retries_and_active_completion_conflicts(self):
        test_graphs.TaskGraphTests.configure_workflow(self, right_command="exit 8")
        self.cli("run")
        self.settle()
        row = self.report("sample")["sample"]
        self.assertReason(row, "retry", "jobs_require_retry")
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["progress"], {"completed": 2, "total": 5})
        active = self.report("sample", env=self.inject(running_prefix="sample__gwflow_complete"))["sample"]
        self.assertReason(active, "blocked", "activity_conflict")
        self.assertEqual(active["attempt_id"], row["attempt_id"])

    def test_activity_and_uncertainty_are_distinct_blockers(self):
        self.run_complete()
        for job, backend_state, state in (("gwflow_prepare", "RUNNING", "preparing"),
                                          ("compute", "RUNNING", "running"),
                                          ("gwflow_complete", "RUNNING", "finishing"),
                                          ("compute", "SUBMITTED", "queued")):
            with self.subTest(state=state):
                active = self.report("a", env=self.inject(job_states={"a__" + job: backend_state}))["a"]
                self.assertReason(active, "wait", "jobs_active")
                self.assertEqual(active["state"], state)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a", "printf UPDATED_A"))
        active = self.report("a", env=self.inject(running_prefix="a__compute"))["a"]
        self.assertReason(active, "blocked", "activity_conflict")
        uncertain = self.report("a", env=self.inject(conflicting_tracking_prefix="a__compute"))["a"]
        self.assertReason(uncertain, "blocked", "submission_unresolved")
        self.assertIsNone(uncertain["progress"]["completed"])

    def test_consumer_uncertainty_does_not_claim_known_activity(self):
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a", "printf UPDATED_A"))
        active = self.report("a", env=self.inject(queued_prefix="c__compute"))["a"]
        self.assertReason(active, "blocked", "activity_conflict")
        uncertain = self.report("a", env=self.inject(conflicting_tracking_prefix="c__compute"))["a"]
        self.assertReason(uncertain, "blocked", "submission_unresolved")

    def test_invalid_baseline_and_unavailable_image_have_distinct_reasons(self):
        self.run_complete()
        path = record_path(self.work, "a", "inputs.json")
        baseline = read_record(path)
        write_record(path, {**baseline, "inputs": None})
        row = self.report("a")["a"]
        self.assertReason(row, "blocked", "validation_failed")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("outputs=['private.txt'])", "outputs=['private.txt'], image='missing.sif')", 1))
        row = self.report("a")["a"]
        self.assertReason(row, "blocked", "image_unavailable")
        self.assertIsNone(row["progress"]["completed"])

    def test_completion_failure_requires_finish_without_repeating_computation(self):
        self.run_complete()
        for backend_state, state in (("FAILED", "failed"), ("CANCELLED", "canceled")):
            with self.subTest(state=state):
                row = self.report("a", env=self.inject(job_states={"a__gwflow_complete": backend_state}))["a"]
                self.assertReason(row, "finish", "completion_pending")
                self.assertEqual(row["state"], state)
                self.assertEqual(row["progress"], {"completed": 2, "total": 3})

    def test_missing_command_evidence_requires_definition_refresh(self):
        self.run_complete()
        before = self.report("a")["a"]
        remove_record(record_path(self.work, "a", "executions/*/definition.json"))
        row = self.report("a")["a"]
        self.assertReason(row, "run", "definition_requires_refresh")
        self.assertEqual(row["attempt_id"], before["attempt_id"])


