"""Readable managed status through the installed CLI and local backend."""

import re
import shlex

from support import TASK_FACTORY, LocalBackendTestCase
import test_fresh
import test_graphs


class ManagedStatusTests(LocalBackendTestCase):
    workers = 1
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_fresh.FreshAttemptTests.inject

    def test_details_nest_targets_in_one_table_without_diagnostics(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        output = self.cli_result("status", "--details").stdout
        self.assertRegex(output, r"Task\s+Target\s+State\s+Jobs completed\s+Detail")
        self.assertEqual(output.count("Jobs completed"), 1)
        self.assertIn("3 of 3 Tasks selected\n3 pending", output)
        sections = re.split(r"^Task [abc]\s+pending\s+0/3\s*$", output, flags=re.MULTILINE)
        self.assertEqual(len(sections), 4)
        for section in sections[1:]:
            jobs = re.findall(r"^\s+([|`]-) (\S+)\s+(\S+)\s*$", section, re.MULTILINE)
            self.assertEqual(jobs, [("|-", "[preparation]", "pending"),
                                    ("|-", "compute", "pending"),
                                    ("`-", "[completion]", "pending")])
        for label in ("Details for Task", "Next action:", "Reason:", "Attempt:",
                      "Workspace:", "Results:", "Backend job:", "Log stderr:"):
            self.assertNotIn(label, output)
        explanation = self.cli_result("explain", "--details").stdout
        self.assertIn("Next action: Run", explanation)
        self.assertIn("Attempt:", explanation)

    def test_expanded_terminal_and_plain_tables_keep_targets_beside_states(self):
        for width in (80, 110, 140):
            for color, options, branch in (("--no-color", (), "├─"), ("--use-color", (), "├─"),
                                           ("--no-color", ("--plain",), "|-")):
                with self.subTest(width=width, color=color, options=options):
                    output = self.terminal_cli(color, "status", "--details", *options, width=width).stdout
                    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
                    self.assertRegex(text, r"Task\s+Target\s+State\s+Jobs completed")
                    self.assertRegex(text, re.escape(branch) + r" left\s+(?:○ )?pending")
                    self.assertLess(text.index("sample"), text.index("[preparation]"))
                    self.assertIn("[completion]", text)
                    self.assertNotIn("Attempt:", text)
                    if color == "--no-color":
                        self.assertNotIn("\x1b[", output)
                    else:
                        self.assertIn("\x1b[", output)

    def test_expanded_long_target_names_wrap_only_when_requested(self):
        name = "left_" + "X" * 100 + "_tail"
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("'left'", repr(name)))
        for options in ((), ("--plain",)):
            with self.subTest(options=options):
                truncated = self.terminal_cli("--no-color", "status", "--details", *options, width=100).stdout
                self.assertIn("…", truncated)
                self.assertNotIn("_tail", truncated)
                self.assertIn("pending", truncated)
                wrapped = self.terminal_cli("--no-color", "status", "--details", *options,
                                            "--no-truncate", width=100).stdout
                self.assertIn("_tail", wrapped)
                self.assertEqual(wrapped.count("X"), 100)
        redirected = self.cli_result("status", "--details").stdout
        self.assertIn(name, redirected)
        self.assertNotIn("…", redirected)

    def test_instances_are_compact_and_task_selection_expands_local_names(self):
        output = self.cli("status", "--instances")
        self.assertIn("Jobs completed", output)
        self.assertRegex(output, r"Task sample\s+pending\s+0/5")
        self.assertIn("1 of 1 Tasks selected", output)
        self.assertIn("1 pending", output)
        self.assertNotIn("left", output)
        self.assertNotIn("next:", output)
        selected = self.cli("status", "task:sample")
        for name in ("[preparation]", "left", "right", "join", "[completion]"):
            self.assertRegex(selected, re.escape(name) + r"\s+pending")
        self.assertLess(selected.index("[preparation]"), selected.index("left"))
        self.assertLess(selected.index("join"), selected.index("[completion]"))
        self.assertNotIn("Attempt:", selected)
        self.assertRegex(self.cli("status", "sample__gwflow_prepare"), r"\[preparation\]\s+pending")

    def test_failed_task_stays_compact_and_matches_primary_filter_before_retry(self):
        self.configure_workflow(right_command="exit 8")
        self.cli("run")
        self.settle()
        output = self.cli("status", "--instances", "--status", "failed")
        self.assertRegex(output, r"Task sample\s+failed\s+2/5\s+.*retry available")
        self.assertNotIn("right", output)
        self.assertIn("1 failed", output)
        self.assertNotIn("Attempt:", output)
        self.assertNotIn("__gwflow_", output)
        details = self.cli("status", "--details")
        self.assertRegex(details, r"right\s+failed")
        self.assertNotIn("Backend job:", details)
        explanation = self.cli("explain", "--details")
        self.assertIn("Next action: Retry", explanation)
        for label in ("Reason:", "Attempt:", "Workspace:", "Backend job:", "Log stderr:"):
            self.assertIn(label, explanation)

    def test_active_targets_distinguish_running_and_submitted(self):
        held, release = self.work / "held", self.work / "release"
        self.configure_workflow(left_command=(
            f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; "
            "do sleep 0.025; done; printf left > same.txt"
        ))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            output = self.cli("status", "--instances")
            self.assertRegex(output, r"Task sample\s+running")
            self.assertNotIn("left", output)
            self.assertIn("1 running", output)
            self.assertIn("1 job running", output)
            selected = self.cli("status", "sample__right", "--status", "running")
            self.assertRegex(selected, r"right\s+queued")
            self.assertIn("left", selected)
            self.assertIn("join", selected)
            self.assertNotIn("sample", self.cli("status", "--instances", "--status", "failed"))
        finally:
            release.touch()
        self.finish()

    def test_terminal_layout_plain_redirection_and_no_color_are_distinct(self):
        decorated = self.terminal_cli("--use-color", "status", "--instances").stdout
        self.assertIn("\x1b[", decorated)
        self.assertIn("╭", decorated)
        self.assertIn("○", decorated)
        self.assertIn("━", decorated)
        self.assertIn("0/5", decorated)
        monochrome = self.terminal_cli("--no-color", "status", "--instances").stdout
        self.assertNotIn("\x1b[", monochrome)
        self.assertIn("╭", monochrome)
        self.assertIn("○", monochrome)
        plain = self.terminal_cli("--use-color", "status", "--instances", "--plain").stdout
        redirected = self.cli_result("--use-color", "status", "--instances").stdout
        for output in (plain, redirected):
            self.assertNotIn("\x1b[", output)
            self.assertNotRegex(output, "[╭○━]")
            self.assertRegex(output, r"Task sample\s+pending\s+0/5")

    def test_formats_group_patterns_and_empty_selections(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "gwf.task", "left.group = 'mapping'\nright.group = 'mapping'\ngwf.task"))
        for output_format in ("default", "summary", "grouped"):
            self.assertIn("--format is only supported for ordinary gwf", self.cli(
                "status", "--instances", "--format", output_format, success=False))
        grouped = self.cli("status", "--details", "--group", "map*")
        self.assertRegex(grouped, r"Task sample\s+pending\s+0/5")
        self.assertIn("left", grouped)
        self.assertIn("right", grouped)
        self.assertIn("join", grouped)
        self.assertIn("0 of 1 Tasks selected", self.cli("status", "absent*"))
        self.assertIn("0 of 1 Tasks selected", self.cli("status", "--details", "absent*"))

    def test_reuse_after_cleanup_does_not_display_failed_or_pending_targets(self):
        self.run_complete()
        self.cli("clean-work", "--delete")
        output = self.cli("status", "--instances")
        self.assertRegex(output, r"Task sample\s+reusable\s+5/5\s+work cleaned")
        self.assertNotIn("left", output)
        expanded = self.cli("status", "task:sample")
        self.assertRegex(expanded, r"left\s+completed")
        self.assertNotIn("failed", expanded)
        self.assertIn("1 reusable", output)

    def test_endpoints_use_task_dependencies_and_fresh_work_hides_old_completion(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        output = self.cli("status", "--instances", "--endpoints")
        self.assertNotIn("Task a", output)
        self.assertIn("Task b", output)
        self.assertIn("Task c", output)
        self.assertIn("2 of 3 Tasks selected", output)
        self.configure(use_spec_hashes=True)
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a", "printf changed"))
        output = self.cli("status", "task:a")
        self.assertRegex(output, r"Task a\s+pending\s+0/3\s+fresh attempt required")
        self.assertRegex(output, r"compute\s+pending")
        self.assertNotIn("reusable", output)

    def test_repair_reopens_completion_and_deferral_keeps_prior_progress(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        self.run_complete()
        (self.work / "results/a/result.txt").unlink()
        output = self.cli("status", "--instances")
        self.assertRegex(output, r"Task a\s+repairable\s+2/3")
        self.assertRegex(output, r"Task b\s+reusable\s+3/3")
        self.assertRegex(output, r"Task c\s+deferred\s+3/3\s+run again after upstream result recovery")
        self.assertIn("1 repairable, 1 reusable, 1 deferred", output)
        self.assertIn("1 of 3 Tasks selected\n1 deferred", self.cli("status", "--instances", "--status", "deferred"))

    def test_observed_lifecycle_execution_and_queues_determine_primary_phase(self):
        self.run_complete()
        for local, state in (("gwflow_prepare", "preparing"), ("left", "running"),
                             ("gwflow_complete", "finishing")):
            with self.subTest(local=local):
                output = self.cli("-b", "recovery_fixture", "status", "--instances",
                                  env=self.inject(running_prefix="sample__" + local))
                self.assertRegex(output, rf"Task sample\s+{state}\s+4/5")
                self.assertIn(f"1 {state}", output)
                output = self.cli("-b", "recovery_fixture", "status", "--instances",
                                  env=self.inject(queued_prefix="sample__" + local))
                self.assertRegex(output, r"Task sample\s+queued\s+4/5")
                self.assertIn("1 queued", output)
                self.assertNotIn("1 active", output)

    def test_known_fresh_progress_stays_zero_when_a_consumer_blocks_replacement(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        self.configure(use_spec_hashes=True)
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a", "printf updated"))
        output = self.cli("-b", "recovery_fixture", "status", "--instances",
                          env=self.inject(queued_prefix="c__compute"))
        self.assertRegex(output, r"Task a\s+blocked\s+0/3\s+.*fresh attempt required")
        self.assertNotIn("progress unavailable", output)

    def test_fresh_blocked_by_own_running_work_keeps_activity_visible(self):
        held, release = self.work / "held", self.work / "release"
        self.configure(use_spec_hashes=True)
        self.configure_workflow(left_command=(
            f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; "
            "do sleep 0.025; done; printf left > same.txt"
        ))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            workflow = self.work / "workflow.py"
            original = workflow.read_text()
            for before, after in (("printf left", "printf updated"), ("'left'", "'renamed'")):
                with self.subTest(change=before):
                    workflow.write_text(original.replace(before, after))
                    output = self.cli("status", "--instances")
                    self.assertRegex(output, r"Task sample\s+blocked\s+0/5")
                    self.assertIn("fresh attempt required; 1 job still running", output)
                    self.assertNotIn("1 active", output)
        finally:
            release.touch()
        self.finish()

    def test_missing_image_has_unknown_progress_with_declared_denominator(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "outputs=['same.txt'])", "outputs=['same.txt'], image='missing.sif')", 1))
        output = self.cli("status", "--instances")
        self.assertRegex(output, r"Task sample\s+blocked\s+\?/5")
        self.assertIn("progress unavailable", output)

    def test_failure_and_blockage_take_precedence_over_running_jobs(self):
        self.configure_workflow(right_command="exit 8")
        self.cli("run")
        self.settle()
        output = self.cli("-b", "recovery_fixture", "status", "--instances",
                          env=self.inject(running_prefix="sample__left"))
        self.assertRegex(output, r"Task sample\s+failed\s+1/5")
        self.assertIn("1 job still running", output)
        self.assertIn("1 failed", output)
        self.assertNotIn("1 active", output)
        env = self.inject(running_prefix="sample__join")
        output = self.cli("-b", "recovery_fixture", "status", "--instances", env=env)
        self.assertRegex(output, r"Task sample\s+blocked\s+2/5")
        self.assertIn("2 jobs failed; 1 job still running", output)
        self.assertIn("1 blocked", output)
        self.assertIn("0 of 1 Tasks selected", self.cli(
            "-b", "recovery_fixture", "status", "--instances", "--status", "failed", env=env))

    def test_canceled_submission_remains_distinct_from_failure(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__left"), success=False)
        self.finish()
        env = self.inject(job_states={"sample__left": "CANCELLED"})
        output = self.cli("-b", "recovery_fixture", "status", "--instances", "--status", "canceled", env=env)
        self.assertRegex(output, r"Task sample\s+canceled\s+1/5")
        self.assertIn("1 job canceled", output)
        self.assertNotIn("1 failed", output)

    def test_canceled_task_keeps_running_sibling_visible_without_double_counting(self):
        held, release = self.work / "held", self.work / "release"
        self.configure_workflow(left_command=(
            f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; "
            "do sleep 0.025; done; printf left > same.txt"
        ))
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().split("join = task.target")[0] +
                            "task.retain('left', source=left.output('same.txt'), path='result.txt')\n"
                            "gwf.task(task, alias='sample')\n")
        self.cli("run")
        self.wait_for(held.exists)
        try:
            environment = self.inject(job_states={"sample__right": "CANCELLED"})
            output = self.cli_result("-b", "recovery_fixture", "status", "--instances", env=environment).stdout
            self.assertRegex(output, r"Task sample\s+canceled\s+1/4")
            self.assertIn("1 of 1 Tasks selected\n1 canceled", output)
            self.assertIn("1 job canceled; 1 job still running", output)
            self.assertNotIn("1 active", output)
            self.assertNotIn("1 failed", output)
            selected = self.cli_result("-b", "recovery_fixture", "status", "sample__right",
                                       "--status", "canceled", env=environment).stdout
            self.assertRegex(selected, r"Task sample\s+canceled\s+1/4")
            for name, state in (("[preparation]", "completed"), ("left", "running"),
                                ("right", "canceled"), ("[completion]", "queued")):
                self.assertRegex(selected, re.escape(name) + r"\s+" + state)
            for state in ("failed", "running", "queued"):
                output = self.cli_result("-b", "recovery_fixture", "status", "--instances", "--status", state,
                                         env=environment).stdout
                self.assertIn("0 of 1 Tasks selected", output)
        finally:
            release.touch()
        self.finish()

    def test_dependency_order_uses_declaration_order_for_eligible_ties(self):
        (self.work / "workflow.py").write_text(
            TASK_FACTORY + "from gwflow import Task, Workflow\n"
            "from gwflow.workflow import RetainedOutput\n"
            "gwf = Workflow()\n"
            "for name in ('z_independent', 'consumer', 'source', 'a_independent'):\n"
            "    inputs = [RetainedOutput(gwf, 'source', 'value')] if name == 'consumer' else []\n"
            "    task = empty_task(inputs=inputs)\n"
            "    target = task.target('compute', inputs=inputs, outputs=['out.txt'])\n"
            "    target << 'touch out.txt'\n"
            "    task.retain('value', source=target.output('out.txt'), path='out.txt')\n"
            "    gwf.task(task, alias=name)\n"
        )
        for command in (("status", "--instances",), ("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                output = self.cli_result(*command).stdout
                names = re.findall(r"^Task (\S+)", output, re.MULTILINE)
                self.assertEqual(names, ["z_independent", "source", "consumer", "a_independent"])
        self.finish()
