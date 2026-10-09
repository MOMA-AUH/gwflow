"""Invocation submission outcomes through the installed CLI and backend seam."""

import json
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_fresh


class SubmissionReportTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle

    def acceptances(self):
        path = self.work / "backend-acceptances.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_normal_summary_counts_new_lifecycle_jobs_and_receiving_tasks(self):
        environment = self.inject(record_acceptances=True)
        result = self.cli_result("-b", "recovery_fixture", "run", env=environment)
        self.assertIn("Submitted 9 jobs across 3 Tasks.", result.stdout)
        self.assertNotIn("Submitted 9 jobs", result.stderr)
        accepted = self.acceptances()
        self.assertEqual(len(accepted), 9)
        for name in ("a", "b", "c"):
            for local in ("gwflow_prepare", "compute", "gwflow_complete"):
                self.assertEqual(sum(item["name"].startswith(f"{name}__{local}__") for item in accepted), 1)
        self.finish()
        result = self.cli_result("-b", "recovery_fixture", "run", env=environment)
        self.assertIn("No new jobs were submitted.", result.stdout)
        self.assertEqual(self.acceptances(), accepted)

    def test_lost_acknowledgement_reports_uncertainty_without_rolling_back_acceptance(self):
        environment = self.inject(record_acceptances=True, lose_ack="a__compute")
        result = self.cli_result("-b", "recovery_fixture", "run", env=environment, success=False)
        self.assertIn("Submission interrupted: 1 job confirmed submitted across 1 Task; "
                      "1 submission outcome unknown; 7 planned jobs not attempted.", result.stdout)
        self.assertIn("Already submitted jobs may continue", result.stdout)
        self.assertNotIn("No new jobs were submitted", result.stdout)
        self.assertNotIn("Submission interrupted", result.stderr)
        self.assertIn("lost acknowledgement after acceptance", result.stderr)
        self.assertEqual(len(self.acceptances()), 2)
        self.settle()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["a"])
        environment = self.inject(record_acceptances=True)
        resumed = self.cli_result("-b", "recovery_fixture", "run", env=environment)
        self.assertIn("Submitted 7 jobs across 3 Tasks.", resumed.stdout)
        self.assertEqual(len(self.acceptances()), 9)
        self.finish()
        self.assertEqual((self.work / "trace").read_text().splitlines().count("a"), 1)

    def test_dry_run_and_global_blockage_never_claim_submission(self):
        environment = self.inject(record_acceptances=True)
        result = self.cli_result("-b", "recovery_fixture", "run", "--dry-run", env=environment)
        self.assertNotIn("Submitted ", result.stdout)
        self.assertEqual(self.acceptances(), [])
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("b = gwf.task",
                            "target.image = 'missing.sif'\nb = gwf.task"))
        for options in ((), ("--dry-run",)):
            result = self.cli_result("-b", "recovery_fixture", "run", *options, env=environment, success=False)
            self.assertIn("Workflow blocked — no new jobs will be submitted", result.stdout)
            self.assertNotIn("Submitted ", result.stdout)
            self.assertNotIn("Submission interrupted", result.stdout)
            self.assertIn("image unavailable", result.stderr)
            self.assertEqual(self.acceptances(), [])

    def test_preflight_failure_and_entered_backend_failure_have_different_certainty(self):
        for injection, unknown, unattempted in (({"reject_before_admission": True}, 0, 9),
                                                ({"reject_prefix": "a__gwflow_prepare"}, 1, 8)):
            with self.subTest(injection=injection):
                environment = self.inject(record_acceptances=True, **injection)
                result = self.cli_result("-b", "recovery_fixture", "run", env=environment, success=False)
                self.assertIn("0 jobs confirmed submitted across 0 Tasks", result.stdout)
                self.assertIn(f"{unknown} submission outcome{'s' if unknown != 1 else ''} unknown", result.stdout)
                self.assertIn(f"{unattempted} planned jobs not attempted", result.stdout)
                self.assertEqual(self.acceptances(), [])

    def test_partial_submission_counts_only_receiving_tasks_and_wraps_critical_summary(self):
        environment = self.inject(record_acceptances=True, reject_prefix="b__gwflow_prepare")
        result = self.terminal_cli("--no-color", "-b", "recovery_fixture", "run",
                                   env=environment, width=32, success=False)
        output = " ".join(result.stdout.replace("│", "").split())
        self.assertIn("3 jobs confirmed submitted across 1 Task", output)
        self.assertIn("1 submission outcome unknown; 5 planned jobs not attempted", output)
        self.assertIn("Already submitted jobs may continue; submission was not rolled back", output)
        self.assertEqual(len(self.acceptances()), 3)
        self.finish()
        self.assertTrue((self.work / "results/a/result.txt").is_file())
        self.assertFalse((self.work / "results/b/result.txt").exists())

    def test_keyboard_interrupt_reports_uncertainty_on_stdout_and_keeps_accepted_work(self):
        environment = self.inject(record_acceptances=True, interrupt_after_acceptance="a__compute")
        result = self.cli_result("-b", "recovery_fixture", "run", "--details", env=environment, success=False)
        self.assertIn("1 job confirmed submitted across 1 Task; 1 submission outcome unknown; "
                      "7 planned jobs not attempted", result.stdout)
        self.assertIn("a__compute: outcome unknown", result.stdout)
        self.assertIn("Aborted", result.stderr)
        self.assertNotIn("Submission interrupted", result.stderr)
        self.assertEqual(len(self.acceptances()), 2)
        self.settle()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["a"])

    def test_acknowledgement_storage_failure_keeps_confirmed_acceptance_and_id(self):
        environment = self.inject(record_acceptances=True)
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work),
                                 "ack_write_error", "a", "-b", "recovery_fixture", "run", "--details"],
                                cwd=self.work, env=environment, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1 job confirmed submitted across 1 Task; 0 submission outcomes unknown; "
                      "8 planned jobs not attempted", result.stdout)
        accepted = self.acceptances()
        self.assertEqual(len(accepted), 1)
        self.assertIn("injected acknowledgement storage failure", result.stderr)
        self.assertIn("Submission outcomes:", result.stdout)
        details = result.stdout.split("Submission outcomes:", 1)[1]
        self.assertIn("a__gwflow_prepare: confirmed submitted", details)
        self.assertIn("Backend job: " + str(accepted[0]["id"]), details)
        self.assertIn(accepted[0]["name"], details)
        self.assertIn("b__compute: not attempted", details)
        self.settle()
