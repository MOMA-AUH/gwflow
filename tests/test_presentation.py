"""Presentation contracts at the installed CLI, including terminal capabilities."""

import re

from support import LocalBackendTestCase
import test_graphs
import test_fresh
import test_inspection


class TaskPresentationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    inject = test_graphs.TaskGraphTests.inject
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def test_blocked_image_keeps_job_diagnostics_in_details_on_every_command(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "outputs=['same.txt'])", "outputs=['same.txt'], image='missing.sif')", 1))
        for command in (("status",), ("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                success = command[0] != "run"
                result = self.cli_result(*command, success=success)
                self.assertIn("image unavailable", result.stdout)
                self.assertNotIn("Target left:", result.stdout)
                self.assertNotIn("missing.sif", result.stdout)
                self.assertNotIn(str(self.work), result.stdout)
                self.assertIn("Workflow blocked — no new jobs will be submitted", result.stdout)
                self.assertNotIn("Workflow blocked", result.stderr)
                self.assertNotIn("Submitted target", result.stderr)
                details = self.cli_result(*command, "--details", success=success).stdout
                self.assertIn("State: blocked; Jobs completed: ?/5", details)
                self.assertIn("Target left:", details)
                self.assertIn("missing.sif", details)
                if not success:
                    self.assertIn("image unavailable", result.stderr)
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.assertFalse((self.work / "results").exists())

    def test_damaged_record_keeps_paths_in_details_and_preserves_evidence(self):
        self.run_complete()
        damaged = next((self.work / ".gwf/gwflow").rglob("completion.json"))
        damaged.unlink()
        damaged.mkdir()
        before = self.snapshot()
        for command in (("status",), ("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                success = command[0] != "run"
                compact = self.cli_result(*command, success=success).stdout
                self.assertIn("validation prevents proceeding; see --details", compact)
                self.assertNotIn(str(self.work), compact)
                self.assertNotIn("completion.json", compact)
                detailed = self.cli_result(*command, "--details", success=success).stdout
                self.assertIn(str(damaged), detailed)
                self.assertIn("State: blocked; Jobs completed: 5/5", detailed)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(damaged.is_dir())

    def test_details_on_each_managed_command_show_the_complete_lifecycle(self):
        self.run_complete()
        before = self.snapshot()
        for command in (("status",), ("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                result = self.cli_result(*command, "--details")
                output = result.stdout
                self.assertIn("State: reusable; Jobs completed: 5/5", output)
                jobs = ["[preparation]", "left", "right", "join", "[completion]"]
                positions = [re.search(re.escape(name) + r"\s+completed", output).start() for name in jobs]
                self.assertEqual(positions, sorted(positions))
                for label in ("Attempt:", "Workspace:", "Results:", "Reason:", "Backend job:", "Log stderr:"):
                    self.assertIn(label, output)
                self.assertNotIn("Details for Task", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_terminal_truncation_is_independent_of_plain_and_redirection(self):
        name = "sample_" + "Z" * 120 + "_tail"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'sample'", repr(name)))
        self.run_complete()
        for command in (("status",), ("explain",), ("run", "--dry-run"), ("run",)):
            for options in ((), ("--plain",)):
                with self.subTest(command=command, options=options):
                    shortened = self.terminal_cli("--no-color", *command, *options, width=80).stdout
                    self.assertIn("…", shortened)
                    self.assertNotIn("_tail", shortened)
                    complete = self.terminal_cli("--no-color", *command, *options, "--no-truncate", width=80).stdout
                    self.assertIn("_tail", re.sub(r"\s+", "", complete))
                    self.assertEqual(complete.count("Z"), 120)
            redirected = self.cli_result("--use-color", *command).stdout
            self.assertIn(name, redirected)
            self.assertNotIn("\x1b[", redirected)

    def test_narrow_and_unsupported_terminals_keep_states_and_counts(self):
        self.run_complete()
        for command in (("status",), ("explain",), ("run", "--dry-run"), ("run",)):
            narrow = self.terminal_cli("--no-color", *command, "--details", width=32).stdout
            self.assertIn("reusable", narrow)
            self.assertIn("5/5", narrow)
            self.assertNotIn("━", narrow)
            for environment in ({"TERM": "dumb"}, {"PYTHONIOENCODING": "ascii"}, {"PYTHONIOENCODING": "shift_jis"}):
                with self.subTest(command=command, environment=environment):
                    output = self.terminal_cli("--use-color", *command, "--details", width=40, env=environment).stdout
                    self.assertIn("reusable", output)
                    self.assertIn("5/5", output)
                    self.assertNotRegex(output, "[╭○━\\x1b]")

    def test_plan_color_and_plain_controls_preserve_actions_and_summary(self):
        self.run_complete()
        for command in (("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                decorated = self.terminal_cli("--use-color", *command).stdout
                monochrome = self.terminal_cli("--no-color", *command).stdout
                plain = self.terminal_cli("--use-color", *command, "--plain").stdout
                redirected = self.cli_result("--use-color", *command).stdout
                self.assertIn("\x1b[", decorated)
                self.assertIn("─", monochrome)
                self.assertNotIn("\x1b[", monochrome)
                for output in (plain, redirected):
                    self.assertNotRegex(output, "[─\\x1b]")
                for output in (decorated, monochrome, plain, redirected):
                    self.assertIn("Reuse", output)
                    self.assertIn("sample", output)
                    if command == ("run",):
                        self.assertIn("No new jobs were submitted.", output)

    def test_narrow_expansion_preserves_job_states_beside_long_names(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'left'", repr("job_" + "x" * 80)))
        for options, width in ((("--plain",), 32), ((), 20)):
            with self.subTest(options=options, width=width):
                output = self.terminal_cli("--no-color", "status", "--details", *options, width=width).stdout
                self.assertRegex(output, r"\[preparation\]\s+(?:○ )?pending")
                self.assertRegex(output, r"job_[^\n]*(?:\n\s+)?(?:○ )?pending")
                self.assertRegex(output, r"\[completion\]\s+(?:○ )?pending")

    def test_job_and_group_selection_expand_all_jobs_in_both_inspectors(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("gwf.task_from_template", "left.group = 'mapping'\ngwf.task_from_template"))
        for command in ("status", "explain"):
            for selection in (("sample__left",), ("--group", "map*"), ("sample*",)):
                with self.subTest(command=command, selection=selection):
                    output = self.cli_result(command, *selection).stdout
                    self.assertIn("State: pending; Jobs completed: 0/5", output)
                    for name in ("[preparation]", "left", "right", "join", "[completion]"):
                        self.assertRegex(output, re.escape(name) + r"\s+pending")

    def test_multiple_expanded_tasks_keep_their_jobs_under_named_headings(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        for command in ("status", "explain"):
            output = self.cli_result(command, "--details", "--endpoints").stdout
            self.assertNotIn("Details for Task a:", output)
            first = output.index("Details for Task b:")
            second = output.index("Details for Task c:")
            for section in (output[first:second], output[second:]):
                self.assertLess(section.index("[preparation]"), section.index("compute"))
                self.assertLess(section.index("compute"), section.index("[completion]"))

    def test_required_blockage_notice_stays_complete_in_narrow_output(self):
        name = "sample_" + "x" * 90 + "_tail"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'sample'", repr(name)).replace(
            "outputs=['same.txt'])", "outputs=['same.txt'], image='missing.sif')", 1))
        for options in ((), ("--plain",)):
            output = self.terminal_cli("--no-color", "status", *options, width=32).stdout
            compact = re.sub(r"[\s│]", "", output)
            self.assertIn("Workflowblocked—nonewjobswillbesubmitted.BlockedTasks:" + name, compact)
            self.assertIn("?/5", output)
            self.assertNotIn("━", output)

    def test_required_deferral_reminder_is_not_truncated(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        self.run_complete()
        (self.work / "results/a/result.txt").unlink()
        for width in (40, 110):
            output = self.terminal_cli("--no-color", "status", width=width).stdout
            self.assertIn("run again after upstream result recovery", " ".join(output.split()))

    def test_presentation_options_do_not_add_backend_observations(self):
        self.run_complete()
        environment = self.inject(record_status=True)
        record = self.work / "backend-observations.jsonl"
        before = self.snapshot()
        for command in (("status",), ("explain",), ("run", "--dry-run"), ("run",)):
            record.unlink(missing_ok=True)
            self.cli("-b", "recovery_fixture", *command, env=environment)
            baseline = record.read_text().splitlines()
            self.assertTrue(baseline)
            selections = (("sample__left",), ("--endpoints",)) if command[0] != "run" else ()
            for options in (("--details",), ("--plain",), ("--no-truncate",), *selections):
                with self.subTest(command=command, options=options):
                    record.unlink()
                    self.terminal_cli("-b", "recovery_fixture", *command, *options, env=environment)
                    self.assertEqual(record.read_text().splitlines(), baseline)
                    self.assertEqual(self.snapshot(), before)
