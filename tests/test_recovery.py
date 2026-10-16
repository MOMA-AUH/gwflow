"""Operator assertions resolve current submissions through the installed CLI."""

from collections import Counter
import json
import shutil
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_graphs
import test_managed_recovery
import test_transfer


class OperatorRecoveryTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    launch = test_managed_recovery.ManagedCoordinationTests.launch

    def interrupt_computation(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"crash_before_execution": True}))
        self.cli("-b", "recovery_fixture", "run",
                 env=self.inject(job_fault="sample__right", record_acceptances=True))
        self.settle()

    def test_unacknowledged_preparation_recovers_without_submitting_and_keeps_attempt(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        for flags in ((), ("--force",), ("--dry-run",)):
            output = self.cli("run", *flags, success=False)
            self.assertIn("unresolved submission", output)
        before = self.task_attempts(success=False)["sample"]
        evidence = {path: path.read_bytes() for path in (self.work / ".gwf/gwflow").rglob("*.json")}
        intent_path = next(path for path in evidence if path.name == "intent.json")
        intent = json.loads(evidence[intent_path])
        output = self.cli("--file", "workflow.py", "--backend", "local", "recover", "sample")
        self.assertIn("gwflow --file workflow.py --backend local run sample --dry-run", output)
        self.assertIn(intent["submission"], output)
        self.assertIn("Resolved", output)
        self.assertNotIn("Submitted target", output)
        self.assertFalse((self.work / "trace").exists())
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        self.assertIn("No unresolved submissions", self.cli("recover", "sample"))
        self.assertEqual(self.task_attempts()["sample"], before)
        self.run_complete()
        self.assertEqual(self.task_attempts()["sample"], before)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 1, "right": 1, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_recovery_accepts_unavailable_state_but_new_submissions_need_new_resolution(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        environment = self.inject(fail_status_prefix="sample")
        self.assertIn("Resolved submission", self.cli("-b", "recovery_fixture", "recover", "sample", env=environment))
        self.assertNotIn("Blocked", self.cli("-b", "recovery_fixture", "run", "--dry-run", env=environment))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        self.assertIn("unresolved submission", self.cli("run", success=False))
        self.assertIn("Resolved submission", self.cli("recover", "sample"))
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_recovery_accepts_unavailable_backend_connection(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        self.configure(**{"backend.local.port": 0})
        output = self.cli("recover", "sample")
        self.assertIn("Backend observations unavailable", output)
        self.assertIn("Resolved submission", output)
        self.assertFalse((self.work / "trace").exists())
        self.configure(**{"backend.local.port": self.port})
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_recovery_preserves_successful_siblings_and_records_every_unresolved_submission(self):
        self.interrupt_computation()
        before = self.task_attempts()["sample"]
        accepted = (self.work / "backend-acceptances.jsonl").read_bytes()
        evidence = {path: path.read_bytes() for path in (self.work / ".gwf/gwflow").rglob("*.json")}
        environment = self.inject(job_states={"sample": "UNKNOWN"}, record_acceptances=True,
                                  record_cancellations=True)
        self.assertIn("unresolved submission", self.cli("-b", "recovery_fixture", "run", env=environment, success=False))
        output = self.cli("-b", "recovery_fixture", "recover", "sample", env=environment)
        self.assertEqual(output.count("Resolved submission"), 3, output)
        for local in ("right", "join", "gwflow_complete"):
            self.assertIn(f"sample__{local}__", output)
        self.assertNotIn("sample__left__", output)
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        self.assertEqual((self.work / "backend-acceptances.jsonl").read_bytes(), accepted)
        self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
        self.assertFalse((self.work / "results/sample/result.txt").exists())
        self.assertIn("Retry", self.cli("-b", "recovery_fixture", "run", "--dry-run", env=environment))
        self.assertIn("No unresolved submissions", self.cli("-b", "recovery_fixture", "recover", "sample", env=environment))
        self.cli("run")
        self.settle()
        self.assertEqual(self.task_attempts()["sample"], before)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 1, "right": 1, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_whole_task_activity_refuses_recovery_even_for_a_successful_sibling(self):
        self.interrupt_computation()
        before = {path: path.read_bytes() for path in (self.work / ".gwf/gwflow").rglob("*.json")}
        for local in ("gwflow_prepare", "left", "right", "gwflow_complete"):
            for state in ("SUBMITTED", "RUNNING"):
                with self.subTest(local=local, state=state):
                    environment = self.inject(job_states={f"sample__{local}": state, "sample": "UNKNOWN"},
                                              record_cancellations=True)
                    output = self.cli("-b", "recovery_fixture", "recover", "sample", env=environment, success=False)
                    self.assertIn("whole-Task inactivity", output)
                    self.assertIn(f"sample__{local}__", output)
                    self.assertEqual({path: path.read_bytes() for path in before}, before)
                    self.assertEqual(set((self.work / ".gwf/gwflow").rglob("*.json")), set(before))
                    self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
        self.cli("-b", "recovery_fixture", "recover", "sample", env=self.inject(job_states={"sample": "UNKNOWN"}))
        environment = self.inject(job_states={"sample__right": "RUNNING", "sample": "UNKNOWN"})
        self.assertIn("queued or running", self.cli("-b", "recovery_fixture", "recover", "sample", env=environment, success=False))
        self.assertIn("active work", self.cli("-b", "recovery_fixture", "run", "--force", env=environment, success=False))

    def test_recovered_old_submission_activity_blocks_reuse_replacement_and_cleanup(self):
        self.interrupt_computation()
        self.cli("-b", "recovery_fixture", "recover", "sample", env=self.inject(job_states={"sample": "UNKNOWN"}))
        records = [json.loads(path.read_text()) for path in (self.work / ".gwf/gwflow").rglob("recovery.json")]
        old_right = next(record["job"] for record in records if "__right__" in record["job"])
        self.cli("run")
        self.settle()
        attempt = self.task_attempts()["sample"]
        environment = self.inject(job_states={old_right: "RUNNING"})
        for arguments in (("run",), ("run", "--force"), ("run", "--force", "--dry-run")):
            output = self.cli("-b", "recovery_fixture", *arguments, env=environment, success=False)
            self.assertIn(old_right, output)
        self.assertIn(old_right, self.cli("-b", "recovery_fixture", "clean", "--delete", "sample", env=environment))
        self.assertEqual(self.task_attempts()["sample"], attempt)
        self.assertTrue((self.work / "work/sample" / attempt).exists())
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_blocked_reports_explain_investigation_and_the_inactivity_assertion(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        for arguments in (("status",), ("status", "sample", "--plain"),
                          ("run", "--dry-run"), ("run", "--force", "--plain")):
            with self.subTest(arguments=arguments):
                output = self.cli(*arguments, success=arguments[0] == "status")
                self.assertIn("sample__gwflow_prepare__", output)
                self.assertIn("Investigate", output)
                self.assertIn("whole-Task inactivity", output)
                self.assertIn("gwflow recover sample", output)
                self.assertNotIn("gwflow recover sample --plain", output)

    def test_lost_acknowledgement_and_tracking_recover_without_repeating_checked_work(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"crash_before_execution": True}))
        for index, loss in enumerate(("lose_ack", "lose_tracking"), 1):
            with self.subTest(loss=loss):
                self.cli("-b", "recovery_fixture", "run", "--force",
                         env=self.inject(job_fault="sample__right", **{loss: "sample__right"}), success=False)
                self.settle()
                environment = self.inject(job_states={"sample": "UNKNOWN"})
                self.assertIn("unresolved submission", self.cli("-b", "recovery_fixture", "run", env=environment, success=False))
                output = self.cli("-b", "recovery_fixture", "recover", "sample", env=environment)
                self.assertEqual(output.count("Resolved submission"), 1, output)
                self.assertIn("sample__right__", output)
                self.cli("run")
                self.settle()
                self.assertEqual(Counter((self.work / "trace").read_text().splitlines()),
                                 {"left": index, "right": index, "join": index})
                self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")


    def test_recovery_only_changes_current_attempt_and_accepts_removed_task(self):
        self.run_complete()
        previous = self.task_attempts()["sample"]
        history = {path: path.read_bytes() for path in (self.work / ".gwf/gwflow").rglob("*.json")
                   if previous in path.parts}
        self.assertTrue(history)
        self.cli("-b", "recovery_fixture", "run", "--force",
                 env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        current = self.task_attempts(success=False)["sample"]
        workflow = self.work / "workflow.py"
        declaration = workflow.read_text()
        workflow.write_text("from gwflow import Workflow\ngwf = Workflow()\n")
        output = self.cli("recover", "sample")
        self.assertIn(current, output)
        self.assertNotIn(previous, output)
        self.assertEqual({path: path.read_bytes() for path in history}, history)
        workflow.write_text(declaration)
        self.run_complete()
        self.assertEqual(self.task_attempts()["sample"], current)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 2, "right": 2, "join": 2})


    def interrupted_recovery(self, phase, code, recorded):
        self.interrupt_computation()
        evidence = {path: path.read_bytes() for path in (self.work / ".gwf/gwflow").rglob("*.json")}
        environment = self.inject(job_states={"sample": "UNKNOWN"})
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase,
                                 "sample", "-b", "recovery_fixture", "recover", "sample"], cwd=self.work,
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        resolutions = list((self.work / ".gwf/gwflow").rglob("recovery.json"))
        self.assertEqual(len(resolutions), recorded)
        self.assertIn("unresolved submission", self.cli("-b", "recovery_fixture", "run", "--dry-run",
                                                       env=environment, success=False))
        output = self.cli("-b", "recovery_fixture", "recover", "sample", env=environment)
        self.assertEqual(output.count("Resolved submission"), 3 - recorded)
        self.cli("run")
        self.settle()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 1, "right": 1, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_interruption_before_recovery_publication_does_not_resolve_submissions(self):
        self.interrupted_recovery("before_recovery_record", 112, 0)

    def test_interruption_after_recovery_publication_only_resolves_recorded_submission(self):
        self.interrupted_recovery("after_recovery_record", 113, 1)

    def test_recovery_serializes_with_other_frontend_operations(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        first, first_output = self.launch("-b", "recovery_fixture", "recover", "sample",
                                          env=self.inject(hold_observation=True), filename="first-recovery")
        self.wait_for((self.work / "observation-held").exists)
        second, second_output = self.launch("recover", "sample", filename="second-recovery")
        try:
            self.wait_for(lambda: "Waiting" in second_output.read_text() or second.poll() is not None)
            self.assertIsNone(second.poll(), second_output.read_text())
        finally:
            (self.work / "observation-release").touch()
        first.wait(timeout=10)
        second.wait(timeout=10)
        self.assertEqual(first.returncode, 0, first_output.read_text())
        self.assertEqual(second.returncode, 0, second_output.read_text())
        self.assertEqual(first_output.read_text().count("Resolved submission"), 1)
        self.assertIn("No unresolved submissions", second_output.read_text())
        self.assertFalse((self.work / "trace").exists())

    def test_recovery_requires_one_exact_task_and_unstarted_task_is_a_noop(self):
        for arguments in ((), ("*",), ("missing",), ("sample__left",), ("sample", "another"),
                          ("sample", "--force"), ("sample", "--dry-run"), ("sample", "--reason", "inactive")):
            with self.subTest(arguments=arguments):
                self.cli("recover", *arguments, success=False)
                self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.assertIn("no recorded attempt", self.cli("recover", "sample"))
        self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_recovery_does_not_bypass_changed_commands_or_inputs(self):
        workflow = self.work / "workflow.py"
        original = workflow.read_text().replace("empty_task(inputs=[])", "empty_task(inputs=['input.txt'])")
        workflow.write_text(original)
        self.interrupt_computation()
        attempt = self.task_attempts()["sample"]
        self.cli("-b", "recovery_fixture", "recover", "sample", env=self.inject(job_states={"sample": "UNKNOWN"}))
        workflow.write_text(original.replace("printf right", "printf changed"))
        self.assertIn("changed commands", self.cli("run", "--dry-run", "--details"))
        workflow.write_text(original)
        source = self.work / "input.txt"
        source.write_text("changed external input\n")
        self.assertIn("input metadata changed", self.cli("run", "--dry-run", "--details"))
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.task_attempts()["sample"], attempt)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 2, "right": 1, "join": 1})


class FinishingRecoveryTests(LocalBackendTestCase):
    configure_workflow = test_transfer.TransferRecoveryTests.configure_workflow
    assert_results = test_transfer.TransferRecoveryTests.assert_results
    settle = test_transfer.TransferRecoveryTests.settle
    inject = test_transfer.TransferRecoveryTests.inject
    fault = test_transfer.TransferRecoveryTests.fault
    add_consumer = test_transfer.TransferRecoveryTests.add_consumer

    def test_recovered_old_consumer_submission_still_protects_producer_results(self):
        workflow = self.work / "workflow.py"
        producer_only = workflow.read_text()
        self.add_consumer()
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"crash_before_execution": True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="c__join"))
        self.settle()
        self.cli("-b", "recovery_fixture", "recover", "c", env=self.inject(job_states={"c": "UNKNOWN"}))
        records = [json.loads(path.read_text()) for path in (self.work / ".gwf/gwflow").rglob("recovery.json")]
        old_join = next(record["job"] for record in records if "c__join__" in record["job"])
        self.cli("run")
        self.settle()
        workflow.write_text(producer_only)
        environment = self.inject(job_states={old_join: "RUNNING"})
        for arguments in (("run", "a", "--force", "--dry-run"), ("run", "a", "--force")):
            self.assertIn("Active consumers block replacement: c",
                          self.cli("-b", "recovery_fixture", *arguments, env=environment, success=False))
        self.assert_results(self.work / "results/samples/a/report")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"])

    def test_recovery_finishes_prepared_results_and_interrupted_repair_without_computation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_manifest=True))
        self.settle()
        attempt = self.task_attempts()["a"]
        environment = self.inject(job_states={"a__gwflow_complete": "UNKNOWN"})
        self.assertIn("unresolved submission", self.cli("-b", "recovery_fixture", "run", env=environment, success=False))
        self.assertIn("a__gwflow_complete__", self.cli("-b", "recovery_fixture", "recover", "a", env=environment))
        self.assertIn("Finish", self.cli("run", "--dry-run"))
        self.cli("run")
        self.settle()
        self.assertEqual(self.task_attempts()["a"], attempt)
        self.assert_results(self.work / "results/samples/a/report")
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        environment = self.inject(job_states={"a__gwflow_complete": "UNKNOWN"})
        self.assertIn("unresolved submission", self.cli("-b", "recovery_fixture", "run", env=environment, success=False))
        self.cli("-b", "recovery_fixture", "recover", "a", env=environment)
        self.cli("run")
        self.settle()
        self.assertEqual(self.task_attempts()["a"], attempt)
        self.assert_results(self.work / "results/samples/a/report")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_recovery_does_not_bypass_consumers_or_result_ownership(self):
        self.add_consumer()
        self.run_complete()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").write_text("damaged result")
        self.cli("-b", "recovery_fixture", "run", "a", env=self.fault(crash_during_copy=True))
        self.settle()
        self.cli("-b", "recovery_fixture", "recover", "a",
                 env=self.inject(job_states={"a__gwflow_complete": "UNKNOWN"}))
        environment = self.inject(running_prefix="c")
        self.assertIn("Active consumers block replacement: c",
                      self.cli("-b", "recovery_fixture", "run", "a", env=environment, success=False))
        self.assertEqual((result / "renamed.txt").read_text(), "damaged result")
        outside = self.work / "unowned-results"
        outside.mkdir()
        (outside / "renamed.txt").write_text("unowned content")
        result.rename(self.work / "owned-results-away")
        result.symlink_to(outside, target_is_directory=True)
        self.cli("run", "a", success=False)
        self.assertEqual((outside / "renamed.txt").read_text(), "unowned content")
        self.assertTrue(result.is_symlink())
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"])
