"""Presentation contracts at the installed CLI, including terminal capabilities."""

import re

from support import LocalBackendTestCase
import test_graphs
import test_fresh


class TaskPresentationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    inject = test_graphs.TaskGraphTests.inject

    def test_details_on_each_managed_command_show_the_complete_lifecycle(self):
        self.run_complete()
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

    def test_terminal_truncation_is_independent_of_plain_and_redirection(self):
        name = "sample_" + "x" * 120 + "_tail"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'sample'", repr(name)))
        for options in ((), ("--plain",)):
            with self.subTest(options=options):
                shortened = self.terminal_cli("--no-color", "status", *options, width=80).stdout
                self.assertIn("…", shortened)
                self.assertNotIn("_tail", shortened)
                complete = self.terminal_cli("--no-color", "status", *options, "--no-truncate", width=80).stdout
                self.assertIn("_tail", re.sub(r"\s+", "", complete))
                self.assertEqual(complete.count("x"), 120)
        redirected = self.cli_result("--use-color", "status").stdout
        self.assertIn(name, redirected)
        self.assertNotIn("\x1b[", redirected)

    def test_narrow_and_unsupported_terminals_keep_states_and_counts(self):
        narrow = self.terminal_cli("--no-color", "status", width=32).stdout
        self.assertIn("pending", narrow)
        self.assertIn("0/5", narrow)
        self.assertNotIn("━", narrow)
        for environment in ({"TERM": "dumb"}, {"PYTHONIOENCODING": "ascii"}, {"PYTHONIOENCODING": "shift_jis"}):
            with self.subTest(environment=environment):
                output = self.terminal_cli("--use-color", "status", width=40, env=environment).stdout
                self.assertIn("pending", output)
                self.assertIn("0/5", output)
                self.assertNotRegex(output, "[╭○━\\x1b]")

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
        self.cli("-b", "recovery_fixture", "status", env=environment)
        baseline = record.read_text().splitlines()
        self.assertTrue(baseline)
        for options in (("--details",), ("sample__left",), ("--plain",), ("--no-truncate",)):
            with self.subTest(options=options):
                record.unlink()
                self.terminal_cli("-b", "recovery_fixture", "status", *options, env=environment)
                self.assertEqual(record.read_text().splitlines(), baseline)
