"""Ordinary whole-workflow explanation through the installed gwf CLI."""

import os
import re

from gwf import Target
from gwf.conf import FileConfig
from gwf.core import get_spec_hashes

import test_reuse


class ExplainCliTests(test_reuse.LocalBackendTestCase):
    def evidence(self):
        return {
            path.relative_to(self.work): (path.read_bytes(), path.stat().st_mtime_ns)
            for directory in ("gwflow", "logs")
            for path in (self.work / ".gwf" / directory).rglob("*")
            if path.is_file()
        }

    def submissions(self, output, prefix="Would submit"):
        return set(re.findall(rf"{prefix} (\w+)", output))

    def target_detail(self, output, name):
        for block in re.split(r"\b(?:target|Completion job)\s+", output):
            if (re.match(rf"{re.escape(name)}\b", block)
                    and "Current:" in block and "Planned:" in block):
                return " ".join(block.lower().split())
        self.fail(f"No state and treatment for {name}: {output}")

    def test_details_show_every_target_state_and_planned_treatment(self):
        output = self.cli("explain", "--details")
        for name in ("text__prepare", "text__finish", "text__gwflow_complete"):
            detail = self.target_detail(output, name)
            self.assertIn("unknown", detail)
            self.assertIn("submit", detail)
        self.assertIn("bookkeeping Completion job", output)
        self.assertEqual(self.submissions(output), self.submissions(self.cli("run", "--dry-run")))

        self.run_complete()
        (self.work / "result.txt").unlink()
        output = self.cli("explain", "--details")
        self.assertIn("up to date", self.target_detail(output, "text__prepare"))
        self.assertIn("submit", self.target_detail(output, "text__finish"))
        self.run_complete()
        (self.work / "middle.txt").unlink()
        before = self.evidence()
        output = self.cli("explain", "--details")
        for name in ("text__prepare", "text__finish", "text__gwflow_complete"):
            detail = self.target_detail(output, name)
            self.assertIn("completed", detail)
            self.assertIn("omitted by reuse", detail)
        self.assertEqual(self.submissions(output), set())
        self.assertEqual(self.evidence(), before)

        for state in ("FAILED", "CANCELLED", "RUNNING", "SUBMITTED"):
            with self.subTest(state=state):
                env = self.state_backend({"text__prepare": state, "text__finish": "RUNNING"})
                output = self.cli("-b", "state_fixture", "explain", "--details", env=env)
                detail = self.target_detail(output, "text__prepare")
                self.assertIn(state.lower(), detail)
                self.assertIn("retry" if state in ("FAILED", "CANCELLED") else "left alone", detail)
                self.assertIn("left alone", self.target_detail(output, "text__finish"))
                self.assertEqual(self.submissions(output), self.submissions(
                    self.cli("-b", "state_fixture", "run", "--dry-run", env=env)))
                self.assertEqual(self.evidence(), before)

    def test_initial_plan_agrees_with_dry_run_and_run_without_writing_evidence(self):
        before = self.evidence()
        output = self.cli("explain")
        self.assertIn("Task text", output)
        self.assertIn("Current:", output)
        self.assertIn("Planned:", output)
        self.assertIn("completion evidence", output.lower())
        expected = {"text__prepare", "text__finish", "text__gwflow_complete"}
        self.assertEqual(self.submissions(output), expected)
        self.assertEqual(self.evidence(), before)
        self.assertFalse((self.work / "trace.txt").exists())
        self.assertEqual(self.submissions(self.cli("run", "--dry-run")), expected)
        self.assertEqual(self.submissions(self.run_complete(), "Submitted target"), expected)

    def test_details_report_independent_direct_causes_together(self):
        self.configure(use_spec_hashes=True)
        self.configure_workflow(side=True, retain_side=True)
        self.run_complete()
        self.configure_workflow(side=True, retain_side=True, boundary_extra=True,
                                inner_extra=True, extra_output=True, command_suffix="changed")
        with (self.work / "workflow.py").open("a") as stream:
            stream.write("gwf.target('seed', inputs=[], outputs=['extra.txt']) << 'touch extra.txt'\n")
        (self.work / "extra.txt").unlink()
        (self.work / "result.txt").unlink()
        stamp = (self.work / "input.txt").stat().st_mtime_ns - 2_000_000_000
        os.utime(self.work / "side.txt", ns=(stamp, stamp))
        for path in (self.work / ".gwf" / "gwflow").rglob("*.json"):
            if path.name != "expected.json":
                path.write_text("invalid record")
        env = self.state_backend({"text__prepare": "FAILED", "text__finish": "RUNNING",
                                  "text__side": "SUBMITTED", "text__gwflow_complete": "CANCELLED"})
        before = self.evidence()
        output = self.cli("-b", "state_fixture", "explain", "--details", env=env)
        for evidence in ("Completion record is unusable", "definition mismatch",
                         "external input is missing", "retained output is missing",
                         "retained output is stale", "saved Completion command hash",
                         "tracked target command", "prevents Reuse"):
            self.assertIn(evidence, output)
        for path in ("extra.txt", "extra-output.txt", "result.txt", "side.txt", "input.txt"):
            self.assertIn(str(self.work / path), output)
        for target, state in (("text__prepare", "failed"), ("text__finish", "running"),
                              ("text__side", "submitted"), ("text__gwflow_complete", "cancelled")):
            self.assertRegex(output, rf"{target}\W+backend\s+{state}\s+prevents\s+Reuse")
        self.assertEqual(self.submissions(output), self.submissions(
            self.cli("-b", "state_fixture", "run", "--dry-run", env=env)))
        concise = self.cli("-b", "state_fixture", "explain", env=env)
        self.assertIn("--details", concise)
        self.assertNotIn("Direct evidence:", concise)
        self.assertEqual(self.evidence(), before)

    def test_details_respect_tracking_modes_and_equal_boundary_mtimes(self):
        for tracking in (False, True):
            with self.subTest(tracking=tracking):
                self.configure(use_spec_hashes=tracking)
                self.configure_workflow(command_suffix="before")
                self.run_complete()
                (self.work / "middle.txt").unlink()
                stamp = (self.work / "result.txt").stat().st_mtime_ns
                (self.work / "input.txt").write_text("changed content\n")
                os.utime(self.work / "input.txt", ns=(stamp, stamp))
                env = self.state_backend({})
                before = self.evidence()
                output = self.cli("-b", "state_fixture", "explain", "--details", env=env)
                self.assertIn("Current: reusable", output)
                self.assertIn("boundary files are up to date", output)
                self.assertIn("command tracking is " + ("enabled" if tracking else "disabled"), output)
                self.assertIn("unknown", self.target_detail(output, "text__prepare"))
                self.assertEqual(self.submissions(output), set())

                self.configure_workflow(command_suffix="after")
                output = self.cli("-b", "state_fixture", "explain", "--details", env=env)
                if tracking:
                    self.assertIn("text__prepare: saved Completion command hash differs", output)
                    self.assertIn("text__prepare: tracked target command changed", output)
                    self.assertIn("old command text cannot be recovered", output)
                else:
                    self.assertIn("Current: reusable", output)
                    self.assertNotIn("command hash differs", output)
                    self.assertNotIn("tracked target command changed", output)
                self.assertEqual(self.submissions(output), self.submissions(
                    self.cli("-b", "state_fixture", "run", "--dry-run", env=env)))
                self.assertEqual(self.evidence(), before)
                self.run_complete()

    def test_details_explain_a_task_without_retained_outputs(self):
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[], outputs=[])\n"
            "task.target('make', inputs=[], outputs=['internal.txt']) << 'touch internal.txt'\n"
            "gwf.task_from_template('scratch', task)\n"
        )
        self.run_complete()
        output = self.cli("explain", "--details")
        self.assertIn("Current: not reusable", output)
        self.assertIn("no retained outputs", output)
        self.assertNotIn("boundary files are up to date", output)
        self.assertEqual(self.submissions(output), self.submissions(self.cli("run", "--dry-run")))

    def test_details_distinguish_missing_and_unusable_completion_evidence(self):
        self.run_complete()
        records = list((self.work / ".gwf" / "gwflow").rglob("*.json"))
        for path in records:
            saved = path.read_bytes()
            for damage in ("missing", "unusable"):
                with self.subTest(path=path.name, damage=damage):
                    if damage == "missing":
                        path.unlink()
                    else:
                        path.write_text("invalid record")
                    before = self.evidence()
                    output = self.cli("explain", "--details")
                    kind = "evidence" if path.name == "expected.json" else "record"
                    self.assertIn(f"Completion {kind} is {damage}", output)
                    self.assertIn(str(path), output)
                    if kind == "evidence":
                        self.assertIn("saved declarations and command hashes are unavailable", output)
                    self.assertIn("completion-only repair", output)
                    self.assertEqual(self.submissions(output), {"text__gwflow_complete"})
                    self.assertEqual(self.evidence(), before)
                    self.assertEqual(self.submissions(output), self.submissions(self.cli("run", "--dry-run")))
                path.write_bytes(saved)

    def test_details_name_changed_declarations_and_target_membership(self):
        self.run_complete()
        self.configure_workflow(boundary_extra=True, retain_middle=True,
                                inner_extra=True, extra_output=True, side=True,
                                prepare_name="renamed")
        before = self.evidence()
        output = self.cli("explain", "--details")
        self.assertIn("definition mismatch", output)
        self.assertIn("text__prepare removed", output)
        self.assertIn("text__renamed added", output)
        self.assertIn("text__side added", output)
        for field, path in (("inputs", "extra.txt"), ("outputs", "middle.txt")):
            self.assertRegex(output, rf"(?s)Task\s+text\s+{field}.*?{re.escape(str(self.work / path))}")
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.submissions(output), self.submissions(self.cli("run", "--dry-run")))
        self.run_complete()
        self.configure_workflow(boundary_extra=True, retain_middle=True,
                                inner_extra=False, extra_output=False, side=True,
                                prepare_name="renamed")
        output = self.cli("explain", "--details")
        for field, path in (("inputs", "extra.txt"), ("outputs", "extra-output.txt")):
            self.assertRegex(output, rf"(?s)text__renamed\s+{field}.*?{re.escape(str(self.work / path))}")

    def test_details_check_gwf_hashes_even_when_completion_hashes_match(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        (self.work / "middle.txt").unlink()
        with get_spec_hashes(working_dir=str(self.work),
                             config=FileConfig.load(self.work / ".gwfconf.json")) as hashes:
            hashes.invalidate(Target("text__prepare", [], [], {}))
        before = self.evidence()
        output = self.cli("explain", "--details")
        self.assertIn("Completion evidence matches", output)
        self.assertIn("text__prepare: tracked target command changed or has no saved hash", output)
        self.assertNotIn("saved Completion command hash differs", output)
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.submissions(output), {"text__prepare", "text__finish", "text__gwflow_complete"})
        self.assertEqual(self.submissions(output), self.submissions(self.cli("run", "--dry-run")))
        self.assertEqual(self.submissions(output), self.submissions(self.run_complete(), "Submitted target"))

    def test_mixed_workflow_identifies_ordinary_targets_and_specific_reasons(self):
        with (self.work / "workflow.py").open("a") as stream:
            stream.write("gwf.target('plain', inputs=['result.txt'], outputs=['plain.txt']) << 'cp result.txt plain.txt'\n")
        output = self.cli("explain")
        self.assertIn("Task text", output)
        self.assertIn("Ordinary target plain", output)
        self.assertIn("dependency work", output)
        self.assertEqual(self.submissions(output), self.submissions(self.cli("run", "--dry-run")))
        self.run_complete()
        (self.work / "plain.txt").unlink()
        before = self.evidence()
        output = self.cli("explain")
        self.assertIn("output is missing", output)
        self.assertEqual(self.submissions(output), {"plain"})
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.submissions(self.run_complete(), "Submitted target"), {"plain"})

    def test_reuse_partial_work_and_completion_repair_agree_with_execution(self):
        for tracking in (False, True):
            with self.subTest(tracking=tracking):
                self.configure(use_spec_hashes=tracking)
                self.run_complete()
                (self.work / "middle.txt").unlink()
                before = self.evidence()
                output = self.cli("explain")
                self.assertIn("Current: reusable", output)
                self.assertIn("no work", output)
                self.assertEqual(self.submissions(output), set())
                self.assertEqual(self.evidence(), before)
                self.assertFalse((self.work / "middle.txt").exists())
                self.assertEqual(self.submissions(self.cli("run", "--dry-run")), set())
                self.assertEqual(self.submissions(self.cli("run"), "Submitted target"), set())

                # Restore the intermediate so the missing retained output needs
                # only its producing target, followed by the Completion job.
                (self.work / "middle.txt").write_text("hello\n")
                (self.work / "result.txt").unlink()
                before = self.evidence()
                output = self.cli("explain")
                expected = {"text__finish", "text__gwflow_complete"}
                self.assertIn("retained output is missing", output)
                self.assertEqual(self.submissions(output), expected)
                self.assertEqual(self.evidence(), before)
                self.assertEqual(self.submissions(self.cli("run", "--dry-run")), expected)
                self.assertEqual(self.submissions(self.run_complete(), "Submitted target"), expected)

                for path in (self.work / ".gwf" / "gwflow").rglob("*.json"):
                    if path.name != "expected.json":
                        path.write_text("invalid record")
                before = self.evidence()
                output = self.cli("explain")
                self.assertIn("completion-only repair", output)
                expected = {"text__gwflow_complete"}
                self.assertEqual(self.submissions(output), expected)
                self.assertEqual(self.evidence(), before)
                self.assertEqual(self.submissions(self.cli("run", "--dry-run")), expected)
                self.assertEqual(self.submissions(self.run_complete(), "Submitted target"), expected)

    def test_selected_workflow_empty_standalone_and_unsupported_workflows(self):
        selected = self.work / "selected.py"
        for source, expected in (
            ("from gwflow import Workflow\nchosen = Workflow()\n", "Empty workflow; no work"),
            ("from gwflow import Workflow\nchosen = Workflow()\n"
             "chosen.target('only', inputs=[], outputs=['only.txt']) << 'touch only.txt'\n",
             "Ordinary target only"),
        ):
            with self.subTest(expected=expected):
                selected.write_text(source)
                output = self.cli("-f", "selected.py:chosen", "explain")
                self.assertIn(expected, output)
                self.assertNotIn("Task text", output)
                self.assertFalse((self.work / "only.txt").exists())
        selected.write_text("from gwf import Workflow\nchosen = Workflow()\n")
        output = self.cli("-f", "selected.py:chosen", "explain", success=False)
        self.assertIn("requires a gwflow.Workflow", output)
        self.assertNotIn("Ordinary whole-workflow plan", output)

    def test_backend_states_gate_task_reuse_with_qualifying_completion_evidence(self):
        self.run_complete()
        (self.work / "middle.txt").unlink()
        before = self.evidence()
        for state in ("FAILED", "CANCELLED", "RUNNING", "SUBMITTED", "UNKNOWN", "COMPLETED"):
            for target in ("text__prepare", "text__gwflow_complete"):
                with self.subTest(state=state, target=target):
                    env = self.state_backend({target: state})
                    output = self.cli("-b", "state_fixture", "explain", "--details", env=env)
                    detail = self.target_detail(output, target)
                    self.assertIn(state.lower(), detail)
                    if state in ("UNKNOWN", "COMPLETED"):
                        self.assertIn("Current: reusable", output)
                        self.assertIn("omitted by reuse", detail)
                        self.assertEqual(self.submissions(output), set())
                    else:
                        self.assertIn("Current: not reusable", output)
                        self.assertIn("left alone" if state in ("RUNNING", "SUBMITTED") else "retry", detail)
                        self.assertIn("prevents Reuse", output)
                        self.assertIn("text__finish", self.submissions(output))
                    self.assertEqual(self.submissions(output), self.submissions(
                        self.cli("-b", "state_fixture", "run", "--dry-run", env=env)))
                    self.assertEqual(self.evidence(), before)

    def test_active_obsolete_attempt_has_no_submissions_but_is_not_reusable(self):
        self.run_complete()
        self.configure_workflow(boundary_extra=True)
        env = self.state_backend({name: "RUNNING" for name in (
            "text__prepare", "text__finish", "text__gwflow_complete",
        )})
        before = self.evidence()
        output = self.cli("-b", "state_fixture", "explain", env=env)
        self.assertIn("Current: not reusable", output)
        self.assertIn("running", output)
        self.assertIn("no new submissions", output)
        self.assertIn("Recovery:", output)
        self.assertIn("text__gwflow_complete", output)
        self.assertIn("later ordinary invocation", output)
        self.assertIn("gwf run", output)
        self.assertEqual(self.submissions(output), set())
        self.assertEqual(self.submissions(output), self.submissions(
            self.cli("-b", "state_fixture", "run", "--dry-run", env=env)))
        self.assertEqual(self.evidence(), before)

        # An ordinary invocation persists the replacement without resubmitting
        # active jobs. With hashes disabled, their attempt is now unidentifiable.
        self.assertEqual(self.submissions(
            self.cli("-b", "state_fixture", "run", env=env), "Submitted target"), set())
        pending = self.evidence()
        self.assertNotEqual(pending, before)
        output = self.cli("-b", "state_fixture", "explain", "--details", env=env)
        self.assertIn("Current: not reusable", output)
        self.assertEqual(self.submissions(output), set())
        self.assertRegex(output, r"Recovery:.*text__gwflow_complete.*later ordinary invocation.*may be needed")
        self.assertEqual(self.evidence(), pending)
        # The old attempt's existing record cannot satisfy the replacement
        # after the backend no longer reports active jobs.
        output = self.cli("-b", "state_fixture", "explain", env=self.state_backend({}))
        self.assertIn("completion-only repair", output)
        self.assertEqual(self.submissions(output), {"text__gwflow_complete"})
        self.assertNotIn("Recovery:", output)
        self.assertEqual(self.evidence(), pending)

    def test_active_changed_command_needs_later_run_even_with_new_completion_job(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        self.configure_workflow(command_suffix="changed")
        env = self.state_backend({"text__prepare": "RUNNING", "text__finish": "COMPLETED",
                                  "text__gwflow_complete": "COMPLETED"})
        before = self.evidence()
        output = self.cli("-b", "state_fixture", "explain", "--details", env=env)
        self.assertIn("left alone", self.target_detail(output, "text__prepare"))
        self.assertRegex(output, r"Recovery:.*text__prepare.*command")
        self.assertIn("later ordinary invocation", output)
        self.assertIn("gwf run", output)
        self.assertEqual(self.submissions(output), {"text__finish", "text__gwflow_complete"})
        self.assertEqual(self.submissions(output), self.submissions(
            self.cli("-b", "state_fixture", "run", "--dry-run", env=env)))
        self.assertEqual(self.evidence(), before)

    def test_backend_query_failure_does_not_display_a_plan(self):
        self.run_complete()
        (self.work / "middle.txt").unlink()
        before = self.evidence()
        for target in ("text__finish", "text__gwflow_complete"):
            for options in ((), ("--details",)):
                with self.subTest(target=target, options=options):
                    env = self.state_backend({target: "ERROR"})
                    output = self.cli("-b", "state_fixture", "explain", *options, env=env, success=False)
                    self.assertIn("Backend query failed", output)
                    self.assertNotIn("Ordinary whole-workflow plan", output)
                    self.assertNotIn("Would submit", output)
                    self.assertEqual(self.evidence(), before)

    def test_command_changes_follow_selected_tracking_configuration(self):
        for tracking in (False, True):
            with self.subTest(tracking=tracking):
                self.configure(use_spec_hashes=tracking)
                self.configure_workflow(command_suffix=f"before {tracking}")
                self.run_complete()
                (self.work / "middle.txt").unlink()
                self.configure_workflow(command_suffix=f"after {tracking}")
                before = self.evidence()
                output = self.cli("explain")
                expected = {"text__prepare", "text__finish", "text__gwflow_complete"} if tracking else set()
                self.assertEqual(self.submissions(output), expected)
                self.assertEqual(self.evidence(), before)
                self.assertEqual(self.submissions(self.cli("run", "--dry-run")), expected)
                self.assertEqual(self.submissions(self.run_complete(), "Submitted target"), expected)

    def test_whole_workflow_validation_precedes_any_plan(self):
        self.run_complete()
        (self.work / "middle.txt").unlink()
        original = (self.work / "workflow.py").read_text()
        cases = (
            ("gwf.target('duplicate', inputs=[], outputs=['result.txt']) << 'touch forbidden'\n",
             "provided by targets"),
            ("from gwflow import Task\n"
             "bad = Task(inputs=['middle.txt'], outputs=['bad.txt'])\n"
             "bad.target('make', inputs=['middle.txt'], outputs=['bad.txt']) << 'touch bad.txt'\n"
             "gwf.task_from_template('bad', bad)\n", "not a retained output"),
            ("from gwflow import Task\n"
             "bad = Task(inputs=['absent.txt'], outputs=['bad.txt'])\n"
             "bad.target('make', inputs=[], outputs=['bad.txt']) << 'touch bad.txt'\n"
             "gwf.task_from_template('bad', bad)\n", "absent.txt"),
            ("gwf.target('cycle', inputs=['result.txt'], outputs=['input.txt']) << 'touch input.txt'\n",
             "depends on itself"),
            ("from gwflow import Task\n"
             "left = Task(inputs=['right.txt'], outputs=['left.txt'])\n"
             "left.target('make', inputs=[], outputs=['left.txt']) << 'touch left.txt'\n"
             "right = Task(inputs=['left.txt'], outputs=['right.txt'])\n"
             "right.target('make', inputs=[], outputs=['right.txt']) << 'touch right.txt'\n"
             "gwf.task_from_template('left', left)\n"
             "gwf.task_from_template('right', right)\n", "depends on itself"),
        )
        before = self.evidence()
        for addition, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                (self.work / "workflow.py").write_text(original + addition)
                output = self.cli("explain", success=False)
                self.assertIn(diagnostic, output)
                self.assertNotIn("Ordinary whole-workflow plan", output)
                self.assertEqual(self.evidence(), before)
        (self.work / "workflow.py").write_text(original)
        (self.work / "input.txt").unlink()
        output = self.cli("explain", success=False)
        self.assertIn("does not exist", output)
        self.assertNotIn("Ordinary whole-workflow plan", output)
        self.assertEqual(self.evidence(), before)

    def test_upstream_work_propagates_from_ordinary_target_through_tasks(self):
        with (self.work / "workflow.py").open("a") as stream:
            stream.write(
                "from gwflow import Task\n"
                "gwf.target('seed', inputs=['extra.txt'], outputs=['input.txt']) << 'cp extra.txt input.txt'\n"
                "report = Task(inputs=['result.txt'], outputs=['report.txt'])\n"
                "report.target('make', inputs=['result.txt'], outputs=['report.txt']) << 'cp result.txt report.txt'\n"
                "gwf.task_from_template('report', report)\n"
            )
        (self.work / "input.txt").unlink()
        self.run_complete()
        (self.work / "middle.txt").unlink()
        stamp = (self.work / "extra.txt").stat().st_mtime_ns - 10_000_000_000
        os.utime(self.work / "input.txt", ns=(stamp, stamp))
        before = self.evidence()
        output = self.cli("explain")
        self.assertIn("upstream work", output)
        expected = {"seed", "text__prepare", "text__finish", "text__gwflow_complete",
                    "report__make", "report__gwflow_complete"}
        self.assertEqual(self.submissions(output), expected)
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.submissions(self.cli("run", "--dry-run")), expected)
        self.assertEqual(self.submissions(self.run_complete(), "Submitted target"), expected)
