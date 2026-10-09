"""Shared intended plans at the installed CLI boundary."""

import re

from support import LocalBackendTestCase
import test_fresh
import test_graphs
import test_transfer


class TaskPlanTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    settle = test_fresh.FreshAttemptTests.settle
    inject = test_fresh.FreshAttemptTests.inject
    fault = test_transfer.TransferRecoveryTests.fault

    def test_fresh_explanation_dry_run_and_run_share_the_same_compact_plan(self):
        explanation = self.cli_result("explain")
        dry_run = self.cli_result("run", "--dry-run")
        actual = self.cli_result("run")
        self.assertEqual(explanation.stdout, dry_run.stdout)
        self.assertEqual(explanation.stdout, actual.stdout.split("Submitted 9 jobs", 1)[0])
        self.assertRegex(explanation.stdout, r"Task\s+Next action\s+Why")
        for name in ("a", "b", "c"):
            self.assertRegex(explanation.stdout, rf"Task {name}\s+Run\s+no completed managed attempt")
        self.assertNotIn("__compute", explanation.stdout)
        self.assertNotIn("Task a", explanation.stderr)
        self.finish()

    def test_blocked_force_keeps_intended_actions_and_complete_conditional_notices(self):
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("b = gwf.task",
                            "target.image = 'missing-image.sif'\nb = gwf.task"))
        before = {str(path): path.read_bytes() for root in (self.work / "results", self.work / ".gwf/gwflow")
                  for path in root.rglob("*") if path.is_file()}
        for command in (("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                result = self.cli_result(*command, "--force-task", "a", success=command[0] == "explain")
                self.assertIn("Workflow blocked — no new jobs will be submitted", result.stdout)
                self.assertRegex(result.stdout, r"Task a\s+Run\s+")
                self.assertRegex(result.stdout, r"Task b\s+Blocked\s+")
                self.assertIn("Would remove previous retained results for Tasks: a, c", result.stdout)
                self.assertIn("if the blocked plan can proceed", result.stdout)
                self.assertNotIn(str(self.work), result.stdout)
                self.assertNotIn("Would remove", result.stderr)
        for options in ((), ("--plain",)):
            output = self.terminal_cli("--no-color", "explain", "--force-task", "a", *options, width=32).stdout
            compact = re.sub(r"[\s│]", "", output)
            self.assertIn("Workflowblocked—nonewjobswillbesubmitted", compact)
            self.assertIn("WouldremovepreviousretainedresultsforTasks:a,c", compact)
            self.assertIn("iftheblockedplancanproceed", compact)
        after = {str(path): path.read_bytes() for root in (self.work / "results", self.work / ".gwf/gwflow")
                 for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_failed_consumer_can_defer_while_producer_plans_repair(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("echo c >>", "exit 8; echo c >>"))
        self.cli("run")
        self.settle()
        (self.work / "results/a/result.txt").unlink()
        explanation = self.cli_result("explain")
        dry_run = self.cli_result("run", "--dry-run")
        self.assertEqual(explanation.stdout, dry_run.stdout)
        self.assertRegex(explanation.stdout, r"Task a\s+Repair\s+retained results missing or changed")
        self.assertRegex(explanation.stdout, r"Task c\s+Defer\s+run again after upstream result recovery")
        details = self.cli_result("explain", "c").stdout
        self.assertIn("State: failed; Jobs completed: 1/3", details)
        self.assertIn("Next action: Defer", details)
        for width in (32, 100):
            output = self.terminal_cli("--no-color", "explain", width=width).stdout
            self.assertIn("run again after upstream result recovery", " ".join(output.split()))

    def test_retry_keeps_queued_completion_followup_visible(self):
        test_graphs.TaskGraphTests.configure_workflow(self, right_command="exit 8")
        self.cli("run")
        self.settle()
        environment = self.inject(queued_prefix="sample__gwflow_complete")
        output = self.cli_result("-b", "recovery_fixture", "explain", env=environment).stdout
        self.assertRegex(output, r"Task sample\s+Retry\s+")
        for width in (32, 100):
            output = self.terminal_cli("--no-color", "-b", "recovery_fixture", "explain",
                                       width=width, env=environment).stdout
            self.assertIn("run again after the queued completion job settles", " ".join(output.split()))

    def test_repair_and_transfer_disclose_only_removal_still_needed(self):
        self.run_complete()
        (self.work / "results/a/result.txt").write_text("manual edit")
        preview = self.cli_result("explain").stdout
        self.assertRegex(preview, r"Task a\s+Repair\s+")
        self.assertIn("Would remove previous retained results for Tasks: a", preview)
        actual = self.cli_result("-b", "recovery_fixture", "run",
                                 env=self.fault(crash_before_installation_intent=True)).stdout
        self.assertIn("Will remove previous retained results for Tasks: a", actual)
        self.assertNotIn("before submitting", actual)
        self.settle()
        preview = self.cli_result("explain").stdout
        self.assertRegex(preview, r"Task a\s+Finish\s+")
        self.assertIn("Would remove previous retained results for Tasks: a", preview)
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        preview = self.cli_result("explain").stdout
        self.assertRegex(preview, r"Task a\s+Finish\s+")
        self.assertNotIn("Would remove", preview)
