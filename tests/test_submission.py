"""Invocation submission outcomes through the installed CLI and backend seam."""

import json
import re
import subprocess
import sys

from rich.console import Console
from rich.text import Text

from support import FIXTURES, LocalBackendTestCase
from terminal_session import TerminalSession, rendered_lines
import test_fresh


class SubmissionReportTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle

    def acceptances(self):
        path = self.work / "backend-acceptances.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_preview_shows_upstream_scope_and_planned_counts_without_acceptance(self):
        result = self.cli_result("-b", "recovery_fixture", "run", "c", "--dry-run",
                                 env=self.inject(record_acceptances=True))
        self.assertIn("Run preview — no jobs will be submitted", result.stdout)
        self.assertIn("Execution scope: 2 Tasks", result.stdout)
        self.assertRegex(result.stdout, r"Task a\s+Run\s+")
        self.assertRegex(result.stdout, r"Task c\s+Run\s+")
        self.assertNotRegex(result.stdout, r"Task b\s+")
        self.assertIn("Would submit 6 jobs across 2 Tasks.", result.stdout)
        self.assertNotIn("Submitted ", result.stdout)
        self.assertEqual(self.acceptances(), [])
        self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_normal_summary_counts_new_lifecycle_jobs_and_receiving_tasks(self):
        environment = self.inject(record_acceptances=True)
        result = self.cli_result("-b", "recovery_fixture", "run", env=environment)
        self.assertTrue(result.stdout.startswith("Execution plan\n"))
        self.assertIn("Planned submissions: 9 jobs across 3 Tasks.", result.stdout)
        self.assertIn("Submitted 9 jobs across 3 Tasks.", result.stdout)
        self.assertNotIn("✓ Submitted", result.stdout)
        self.assertNotIn("Submission outcomes:", result.stdout)
        self.assertNotIn("\x1b[", result.stdout)
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

    def test_terminal_progress_counts_confirmed_acceptances_while_backend_waits(self):
        environment = self.inject(record_acceptances=True, hold_submission_prefix="a__compute")
        terminal = TerminalSession([self.executable, "-b", "recovery_fixture", "run"],
                                   cwd=self.work, env={**environment, "NO_COLOR": ""}, size=(100, 24))
        self.addCleanup(terminal.close)
        try:
            self.wait_for((self.work / "submission-held").exists)
            terminal.wait_for("1/9 confirmed", timeout=3)
            self.assertIn("Submitting jobs", terminal.text)
            self.assertEqual(len(self.acceptances()), 1)
            self.assertNotIn("2/9 confirmed", terminal.text)
        finally:
            (self.work / "submission-release").touch()
        self.assertEqual(terminal.finish(), 0, terminal.text)
        self.assertIn("9/9 confirmed", terminal.text)
        self.assertIn("Submitted 9 jobs across 3 Tasks.", terminal.text)
        visible = rendered_lines(terminal.output.decode())
        self.assertRegex(visible, r"9/9 confirmed[^\n]*\n✓ Submitted 9 jobs across 3 Tasks\.")
        summary = next(line for line in Text.from_ansi(terminal.output.decode().replace('\r\n', '\n')).split('\n')
                       if 'Submitted 9 jobs' in line.plain)
        self.assertEqual(summary.plain, '✓ Submitted 9 jobs across 3 Tasks.')
        console = Console()
        for index, char in enumerate(summary.plain):
            style = summary.get_style_at_offset(console, index)
            self.assertEqual(bool(style.bold), char.isdigit(), (char, style))
            if char == '✓':
                self.assertEqual(style.color.number, 2, 'Only the checkmark uses standard green')
            else:
                self.assertIsNone(style.color, (char, style))
        self.assertEqual(len(self.acceptances()), 9)
        for accepted in self.acceptances():
            self.assertNotIn(accepted["name"], terminal.text)
        self.assertNotIn("ETA", terminal.text)
        self.assertNotIn("100%", terminal.text)
        self.finish()

    def test_lost_acknowledgement_reports_uncertainty_without_rolling_back_acceptance(self):
        environment = self.inject(record_acceptances=True, lose_ack="a__compute")
        result = self.cli_result("-b", "recovery_fixture", "run", env=environment, success=False)
        self.assertIn("Submission interrupted", result.stdout)
        self.assertIn("1 job confirmed submitted across 1 Task", result.stdout)
        self.assertIn("1 submission acceptance unconfirmed", result.stdout)
        self.assertIn("7 planned jobs not attempted", result.stdout)
        self.assertIn("Already submitted jobs may continue", result.stdout)
        self.assertNotIn("No new jobs were submitted", result.stdout)
        self.assertNotIn("Submission interrupted", result.stderr)
        self.assertIn("lost acknowledgement after acceptance", result.stderr)
        self.assertEqual(len(self.acceptances()), 2)
        self.assertIn("a__compute: acceptance unconfirmed", result.stdout)
        self.assertIn(self.acceptances()[1]["name"], result.stdout)
        self.assertNotIn(self.acceptances()[0]["name"], result.stdout)
        self.settle()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["a"])
        environment = self.inject(record_acceptances=True)
        resumed = self.cli_result("-b", "recovery_fixture", "run", env=environment)
        self.assertIn("Submitted 7 jobs across 3 Tasks.", resumed.stdout)
        self.assertEqual(len(self.acceptances()), 9)
        self.finish()
        self.assertEqual((self.work / "trace").read_text().splitlines().count("a"), 1)

    def test_terminal_refresh_preserves_every_backend_warning(self):
        environment = self.inject(record_acceptances=True, submission_warnings=True)
        terminal = TerminalSession([self.executable, "-b", "recovery_fixture", "run", "a"],
                                   cwd=self.work, env=environment, size=(180, 24))
        self.addCleanup(terminal.close)
        self.assertEqual(terminal.finish(), 0, terminal.text)
        visible = rendered_lines(terminal.output.decode())
        for local in ("gwflow_prepare", "compute", "gwflow_complete"):
            self.assertIn("Submission warning for " + local, visible)
        self.assertIn("Submitted 3 jobs across 1 Task.", visible)
        self.assertEqual(len(self.acceptances()), 3)
        self.finish()

    def test_narrow_no_color_bar_never_counts_unconfirmed_acceptance(self):
        environment = self.inject(record_acceptances=True, hold_submission_prefix="a__compute", lose_ack="a__compute")
        terminal = TerminalSession([self.executable, "-b", "recovery_fixture", "run"], cwd=self.work,
                                   env={**environment, "NO_COLOR": "1"}, size=(32, 24))
        self.addCleanup(terminal.close)
        try:
            self.wait_for((self.work / "submission-held").exists)
            terminal.wait_for("1/9 confirmed", timeout=3)
            self.assertEqual(len(self.acceptances()), 1)
        finally:
            (self.work / "submission-release").touch()
        self.assertEqual(terminal.finish(), 1, terminal.text)
        self.assertEqual(len(self.acceptances()), 2)
        self.assertNotIn("2/9 confirmed", terminal.text)
        compact = re.sub(r"\s", "", terminal.text)
        self.assertIn("1submissionacceptanceunconfirmed", compact)
        self.assertIn(self.acceptances()[1]["name"], compact)
        self.assertNotRegex(terminal.output.decode(), r"\x1b\[[0-9;]*m")
        self.assertGreater(terminal.output.rfind(b'\x1b[?25h'), terminal.output.rfind(b'\x1b[?25l'))
        self.settle()

    def test_plain_redirected_and_unsupported_terminals_preserve_reports_without_animation(self):
        environment = self.inject(record_acceptances=True)
        redirected = self.cli_result("-b", "recovery_fixture", "run", "a", env=environment)
        self.assertIn("Submitted 3 jobs across 1 Task.", redirected.stdout)
        self.assertNotIn("✓ Submitted", redirected.stdout)
        self.assertNotIn("Submitting jobs", redirected.stdout)
        self.assertNotIn("\x1b[", redirected.stdout + redirected.stderr)
        self.assertNotIn("Submitted target", redirected.stderr)
        self.assertIn("Inspecting workflow", redirected.stderr)
        self.finish()
        for options, extra in ((("--plain",), {}), ((), {"TERM": "dumb"}),
                               ((), {"PYTHONIOENCODING": "ascii"})):
            with self.subTest(options=options, environment=extra):
                result = self.terminal_cli("-b", "recovery_fixture", "run", "a", "--force", *options,
                                           env={**environment, **extra}, width=40, merge_stderr=True)
                self.assertIn("Submitted 3 jobs across 1 Task.", result.stdout)
                self.assertNotIn("✓ Submitted", result.stdout)
                self.assertNotIn("Submitting jobs", result.stdout)
                self.assertNotIn("\x1b[", result.stdout)
                self.finish()
        debug = self.cli_result("--verbose", "debug", "-b", "recovery_fixture", "run", "a", "--force",
                                env=environment)
        self.assertIn("Submitted 3 jobs across 1 Task.", debug.stdout)
        self.assertIn("Submitted target a__compute", debug.stderr)
        self.assertNotIn("\x1b[", debug.stderr)
        self.assertEqual(len(self.acceptances()), 15)
        self.finish()

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
            self.assertNotIn("Would submit", result.stdout)
            self.assertNotIn("Planned submissions:", result.stdout)
            self.assertIn("image unavailable", result.stderr)
            self.assertEqual(self.acceptances(), [])

    def test_preflight_failure_and_entered_backend_failure_have_different_certainty(self):
        for injection, unknown, unattempted in (({"reject_before_admission": True}, 0, 9),
                                                ({"reject_prefix": "a__gwflow_prepare"}, 1, 8)):
            with self.subTest(injection=injection):
                environment = self.inject(record_acceptances=True, **injection)
                result = self.cli_result("-b", "recovery_fixture", "run", env=environment, success=False)
                self.assertIn("0 jobs confirmed submitted across 0 Tasks", result.stdout)
                self.assertIn(f"{unknown} submission acceptance{'s' if unknown != 1 else ''} unconfirmed", result.stdout)
                self.assertIn(f"{unattempted} planned jobs not attempted", result.stdout)
                state = "acceptance unconfirmed" if unknown else "not attempted; pre-admission failed"
                self.assertIn("a__gwflow_prepare: " + state, result.stdout)
                self.assertRegex(result.stdout, r"Attempt: [0-9a-f]{32}")
                self.assertRegex(result.stdout, r"Submission: a__gwflow_prepare__[0-9a-f]{32}__[0-9a-f]{32}")
                self.assertEqual(self.acceptances(), [])

    def test_partial_submission_counts_only_receiving_tasks_and_wraps_critical_summary(self):
        environment = self.inject(record_acceptances=True, reject_prefix="b__gwflow_prepare")
        result = self.terminal_cli("-b", "recovery_fixture", "run",
                                   env=environment, width=32, success=False, no_color=True)
        output = " ".join(result.stdout.replace("│", "").split())
        self.assertIn("3 jobs confirmed submitted across 1 Task", output)
        self.assertIn("1 submission acceptance unconfirmed", output)
        self.assertIn("5 planned jobs not attempted", output)
        self.assertIn("Already submitted jobs may continue; submission was not rolled back", output)
        self.assertEqual(len(self.acceptances()), 3)
        self.finish()
        self.assertTrue((self.work / "results/a/result.txt").is_file())
        self.assertFalse((self.work / "results/b/result.txt").exists())

    def test_keyboard_interrupt_reports_uncertainty_on_stdout_and_keeps_accepted_work(self):
        environment = self.inject(record_acceptances=True, interrupt_after_acceptance="a__compute")
        result = self.cli_result("-b", "recovery_fixture", "run", "--details", env=environment, success=False)
        self.assertIn("1 job confirmed submitted across 1 Task", result.stdout)
        self.assertIn("1 submission acceptance unconfirmed", result.stdout)
        self.assertIn("7 planned jobs not attempted", result.stdout)
        self.assertIn("a__compute: acceptance unconfirmed", result.stdout)
        self.assertIn("Aborted", result.stderr)
        self.assertNotIn("Submission interrupted", result.stderr)
        self.assertEqual(len(self.acceptances()), 2)
        self.settle()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["a"])

    def test_narrow_partial_submission_preserves_long_ids_and_distinct_outcomes(self):
        name = "a_producer_" + "P" * 80 + "_tail"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("alias='a'", f"alias={name!r}"))
        environment = self.inject(record_acceptances=True, lose_ack=name + "__compute")
        result = self.terminal_cli("-b", "recovery_fixture", "run", "--details",
                                   env=environment, width=32, success=False, no_color=True)
        output = re.sub(r"[\s│]", "", result.stdout)
        self.assertNotIn("…", output)
        self.assertIn("Nextaction:Run", output)
        self.assertIn("1jobconfirmedsubmittedacross1Task", output)
        self.assertIn("1submissionacceptanceunconfirmed", output)
        self.assertIn("7plannedjobsnotattempted", output)
        outcomes = output.split("Submissionoutcomes:", 1)[1]
        self.assertIn(name + "__gwflow_prepare:confirmedsubmitted", outcomes)
        self.assertIn(name + "__compute:acceptanceunconfirmed", outcomes)
        self.assertIn(name + "__gwflow_complete:notattempted", outcomes)
        self.assertIn("b__compute:notattempted", outcomes)
        accepted = self.acceptances()
        self.assertEqual(len(accepted), 2)
        self.assertIn(accepted[0]["name"], outcomes)
        self.assertIn("Backendjob:" + str(accepted[0]["id"]), outcomes)
        self.assertNotIn("Submission interrupted", result.stderr)
        self.settle()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["a"])

    def test_acknowledgement_storage_failure_keeps_confirmed_acceptance_and_id(self):
        environment = self.inject(record_acceptances=True)
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work),
                                 "ack_write_error", "a", "-b", "recovery_fixture", "run"],
                                cwd=self.work, env=environment, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1 job confirmed submitted across 1 Task", result.stdout)
        self.assertIn("0 submission acceptances unconfirmed", result.stdout)
        self.assertIn("8 planned jobs not attempted", result.stdout)
        accepted = self.acceptances()
        self.assertEqual(len(accepted), 1)
        self.assertIn("injected acknowledgement storage failure", result.stderr)
        self.assertIn("Submissions requiring attention:", result.stdout)
        details = result.stdout.split("Submissions requiring attention:", 1)[1]
        self.assertIn("a__gwflow_prepare: confirmed submitted; later submission step failed", details)
        self.assertIn("Backend job: " + str(accepted[0]["id"]), details)
        self.assertIn(accepted[0]["name"], details)
        self.assertNotIn("b__compute: not attempted", details)
        self.settle()

    def test_failure_preparing_a_submission_keeps_its_identity_and_existing_recovery_guard(self):
        environment = self.inject(record_acceptances=True)
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work),
                                 "submission_log_error", "a", "-b", "recovery_fixture", "run"],
                                cwd=self.work, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("a__gwflow_prepare: not attempted; pre-admission failed", result.stdout)
        self.assertRegex(result.stdout, r"Attempt: [0-9a-f]{32}")
        self.assertRegex(result.stdout, r"Submission: a__gwflow_prepare__[0-9a-f]{32}__[0-9a-f]{32}")
        self.assertIn("0 submission acceptances unconfirmed", result.stdout)
        self.assertIn("9 planned jobs not attempted", result.stdout)
        self.assertEqual(self.acceptances(), [])
        self.assertIn("injected submission log alias failure", result.stderr)
        self.assertIn("unresolved submission", self.cli("run", success=False))
        self.assertEqual(self.acceptances(), [])
        self.cli("recover", "a")
        self.cli("-b", "recovery_fixture", "run", env=environment)
        self.assertEqual(len(self.acceptances()), 9)
        self.finish()

    def test_backend_exit_failure_keeps_confirmed_identities_without_duplicate_retry(self):
        result = self.cli_result("-b", "recovery_fixture", "run", success=False,
                                 env=self.inject(record_acceptances=True, fail_tracking_flush=True))
        self.assertIn("9 jobs confirmed submitted across 3 Tasks", result.stdout)
        self.assertIn("0 submission acceptances unconfirmed", result.stdout)
        self.assertIn("0 planned jobs not attempted", result.stdout)
        accepted = self.acceptances()
        self.assertEqual(len(accepted), 9)
        for item in accepted:
            self.assertIn(item["name"], result.stdout)
            self.assertIn("Backend job: " + str(item["id"]), result.stdout)
        self.assertIn("injected backend tracking flush failure", result.stderr)
        self.finish()
        retried = self.cli_result("-b", "recovery_fixture", "run", env=self.inject(record_acceptances=True))
        self.assertIn("No new jobs were submitted.", retried.stdout)
        self.assertEqual(self.acceptances(), accepted)
