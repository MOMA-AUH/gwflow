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
        for command in (("status", "*",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                success = command[0] != "run"
                result = self.cli_result(*command, success=success)
                self.assertIn("image unavailable", result.stdout)
                self.assertNotIn("Target left:", result.stdout)
                self.assertNotIn("missing.sif", result.stdout)
                self.assertNotIn(str(self.work), result.stdout)
                self.assertIn("Workflow blocked — no new jobs will be submitted", result.stdout)
                self.assertNotIn("Workflow blocked", result.stderr)
                self.assertNoSubmissions(result.stderr)
                details = self.cli_result(*command, "--details", success=success).stdout
                if command[0] == "status":
                    self.assertRegex(details, r"(?m)^sample\s+blocked\s+\?/5")
                    self.assertRegex(details, r"left\s+unknown")
                    self.assertNotIn("missing.sif", details)
                else:
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
        for command in (("status", "*",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                success = command[0] != "run"
                compact = self.cli_result(*command, success=success).stdout
                hint = "gwflow run --dry-run --details" if command[0] == "status" else "--details"
                self.assertIn("validation prevents proceeding; see " + hint, compact)
                self.assertNotIn(str(self.work), compact)
                self.assertNotIn("completion.json", compact)
                detailed = self.cli_result(*command, "--details", success=success).stdout
                if command[0] == "status":
                    self.assertNotIn(str(damaged), detailed)
                    self.assertRegex(detailed, r"(?m)^sample\s+blocked\s+5/5")
                else:
                    self.assertIn(str(damaged), detailed)
                    self.assertIn("State: blocked; Jobs completed: 5/5", detailed)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(damaged.is_dir())

    def test_details_on_each_managed_command_show_the_complete_lifecycle(self):
        self.run_complete()
        before = self.snapshot()
        for command in (("status",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                result = self.cli_result(*command, "--details")
                output = result.stdout
                if command[0] == "status":
                    self.assertRegex(output, r"(?m)^sample\s+reusable\s+5/5")
                else:
                    self.assertIn("State: reusable; Jobs completed: 5/5", output)
                jobs = ["[preparation]", "left", "right", "join", "[completion]"]
                positions = [re.search(re.escape(name) + r"\s+completed", output).start() for name in jobs]
                self.assertEqual(positions, sorted(positions))
                for label in ("Attempt:", "Workspace:", "Results:", "Reason:", "Backend job:", "Log stderr:"):
                    if command[0] == "status":
                        self.assertNotIn(label, output)
                    else:
                        self.assertIn(label, output)
                self.assertNotIn("Details for Task", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_complete_names_are_default_across_widths_plain_output_and_redirection(self):
        name = "sample_" + "Z" * 120 + "_tail"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'sample'", repr(name)))
        self.run_complete()
        for command in (("status", "*",), ("run", "--dry-run"), ("run",)):
            for width in (32, 80, 140):
                for options in ((), ("--plain",)):
                    with self.subTest(command=command, width=width, options=options):
                        output = self.terminal_cli(*command, *options, width=width, no_color=True).stdout
                        self.assertNotIn("…", output)
                        self.assertIn("_tail", re.sub(r"\s+", "", output))
                        self.assertEqual(output.count("Z"), 120)
            redirected = self.cli_result(*command).stdout
            self.assertIn(name, redirected)
            self.assertNotIn("\x1b[", redirected)

    def test_narrow_and_unsupported_terminals_keep_states_and_counts(self):
        self.run_complete()
        for command in (("status",), ("run", "--dry-run"), ("run",)):
            narrow = self.terminal_cli(*command, "--details", width=32, no_color=True).stdout
            self.assertIn("reusable", narrow)
            self.assertIn("5/5", narrow)
            if command[0] == "status":
                self.assertRegex(narrow, r"5/5 \[━+\]")
            for environment in ({"TERM": "dumb"}, {"PYTHONIOENCODING": "ascii"}, {"PYTHONIOENCODING": "shift_jis"}):
                with self.subTest(command=command, environment=environment):
                    output = self.terminal_cli(*command, "--details", width=40, env=environment, no_color=False).stdout
                    self.assertIn("reusable", output)
                    self.assertIn("5/5", output)
                    self.assertNotRegex(output, "[╭○━\\x1b]")

    def test_removed_truncation_option_is_rejected_and_absent_from_help(self):
        for command in ("run", "status"):
            with self.subTest(command=command):
                self.assertNotIn("--no-truncate", self.cli(command, "--help"))
                output = self.cli(command, "--no-truncate", success=False)
                self.assertIn("No such option", output)
                self.assertIn("--no-truncate", output)

    def test_detailed_job_names_and_diagnostics_are_complete_in_all_output_modes(self):
        local = "job_" + "X" * 90 + "_tail"
        image = "diagnostic_" + "Q" * 110 + "_image_tail.sif"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'left'", repr(local)).replace(
            "outputs=['same.txt'])", f"outputs=['same.txt'], image={image!r})", 1))
        controls = (("--use-color", (), {}), ("--no-color", (), {}),
                    ("--use-color", ("--plain",), {}),
                    ("--use-color", (), {"PYTHONIOENCODING": "ascii"}))
        for command in (("status",), ("run", "--dry-run"), ("run",)):
            success = command[0] == "status"
            redirected = self.cli_result(*command, "--details", success=success).stdout
            self.assertNotIn("\x1b[", redirected)
            for width in (32, 100):
                for color, options, environment in controls:
                    with self.subTest(command=command, width=width, color=color, options=options, env=environment):
                        raw = self.terminal_cli(*command, "--details", *options,
                                                width=width, env=environment, success=success, no_color=color == "--no-color").stdout
                        output = re.sub(r"\x1b\[[0-9;]*m", "", raw)
                        compact = re.sub(r"\s+", "", output)
                        self.assertNotIn("…", output)
                        self.assertIn("sample__" + local, compact)
                        self.assertEqual(output.count("X"), redirected.count("X"))
                        self.assertEqual(output.count("Q"), redirected.count("Q"))
                        self.assertIn("?/5", compact)
                        self.assertIn("blocked", output)
                        if success:
                            self.assertNotIn(image, compact)
                            self.assertNotIn("Attempt:", output)
                        else:
                            self.assertIn(image, compact)
                            self.assertIn("Nextaction:Blocked", compact)
                            self.assertIn("Reason:Target" + local, compact)
                        if color == "--no-color" or options or environment:
                            self.assertNotIn("\x1b[", raw)
                        if options or environment:
                            self.assertNotRegex(output, "[╭╮╰╯│─━]")

    def test_plan_color_and_plain_controls_preserve_actions_and_summary(self):
        self.run_complete()
        for command in (("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                decorated = self.terminal_cli(*command, no_color=False).stdout
                monochrome = self.terminal_cli(*command, no_color=True).stdout
                plain = self.terminal_cli(*command, "--plain", no_color=False).stdout
                redirected = self.cli_result(*command).stdout
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
                output = self.terminal_cli("status", "--details", *options, width=width, no_color=True).stdout
                self.assertRegex(output, r"\[preparation\]\s+State: pending")
                self.assertIn("sample__job_" + "x" * 80, output)
                self.assertRegex(output, r"job_[^\n]*\n\s+State: pending")
                self.assertRegex(output, r"\[completion\]\s+State: pending")

    def test_details_expand_all_jobs_in_status_and_run_preview(self):
        for command in (("status",), ("run", "--dry-run")):
            output = self.cli_result(*command, "--details").stdout
            if command[0] == "status":
                self.assertRegex(output, r"(?m)^sample\s+pending\s+0/5")
                self.assertNotIn("Attempt:", output)
            else:
                self.assertIn("State: pending; Jobs completed: 0/5", output)
            for name in ("[preparation]", "left", "right", "join", "[completion]"):
                self.assertRegex(output, re.escape(name) + r"\s+pending")

    def test_multiple_expanded_tasks_keep_their_jobs_under_their_owner(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        for command in (("status",), ("run", "--dry-run")):
            output = self.cli_result(*command, "--details").stdout
            prefix = "\n" if command[0] == "status" else "Details for Task "
            positions = [output.index(prefix + name) for name in ("a", "b", "c")]
            for first, last in zip(positions, [*positions[1:], len(output)]):
                section = output[first:last]
                self.assertLess(section.index("[preparation]"), section.index("compute"))
                self.assertLess(section.index("compute"), section.index("[completion]"))

    def test_required_blockage_notice_stays_complete_in_narrow_output(self):
        name = "sample_" + "x" * 90 + "_tail"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'sample'", repr(name)).replace(
            "outputs=['same.txt'])", "outputs=['same.txt'], image='missing.sif')", 1))
        for options in ((), ("--plain",)):
            output = self.terminal_cli("status", "*", *options, width=32, no_color=True).stdout
            compact = re.sub(r"[\s│]", "", output)
            self.assertIn("Workflowblocked—nonewjobswillbesubmitted.BlockedTasks:" + name, compact)
            self.assertIn("?/5", output)
            self.assertNotIn("━", output)

    def test_required_deferral_reminder_is_not_truncated(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        self.run_complete()
        (self.work / "results/a/result.txt").unlink()
        for width in (40, 110):
            output = self.terminal_cli("status", "*", width=width, no_color=True).stdout
            self.assertIn("run again after upstream result recovery", " ".join(output.split()))

    def test_presentation_options_do_not_add_backend_observations(self):
        self.run_complete()
        environment = self.inject(record_status=True)
        record = self.work / "backend-observations.jsonl"
        before = self.snapshot()
        for command in (("status",), ("run", "--dry-run"), ("run",)):
            record.unlink(missing_ok=True)
            self.cli("-b", "recovery_fixture", *command, env=environment)
            baseline = record.read_text().splitlines()
            self.assertTrue(baseline)
            selections = (("sample",), ("*",)) if command[0] != "run" else ()
            for options in (("--details",), ("--plain",), *selections):
                with self.subTest(command=command, options=options):
                    record.unlink()
                    self.terminal_cli("-b", "recovery_fixture", *command, *options, env=environment)
                    self.assertEqual(record.read_text().splitlines(), baseline)
                    self.assertEqual(self.snapshot(), before)
