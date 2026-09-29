"""Ordinary whole-workflow explanation through the installed gwf CLI."""

import os
import re

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

    def test_selected_backend_reports_failed_cancelled_and_active_work_without_submitting(self):
        self.run_complete()
        for state in ("FAILED", "CANCELLED", "RUNNING", "SUBMITTED", "UNKNOWN", "COMPLETED"):
            with self.subTest(state=state):
                env = self.state_backend({"text__prepare": state, "text__finish": "RUNNING"})
                before = self.evidence()
                output = self.cli("-b", "state_fixture", "explain", env=env)
                self.assertIn("running", output)
                if state not in ("UNKNOWN", "COMPLETED"):
                    self.assertIn(state.lower(), output)
                self.assertEqual(self.submissions(output), self.submissions(
                    self.cli("-b", "state_fixture", "run", "--dry-run", env=env)))
                self.assertEqual(self.evidence(), before)
        # UNKNOWN alone must preserve valid Completion evidence after cleanup.
        (self.work / "middle.txt").unlink()
        output = self.cli("-b", "state_fixture", "explain", env=self.state_backend({}))
        self.assertIn("Current: reusable", output)
        self.assertEqual(self.submissions(output), set())

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
        self.assertEqual(self.submissions(output), set())
        self.assertEqual(self.evidence(), before)

    def test_backend_query_failure_does_not_display_a_plan(self):
        env = self.state_backend({"text__finish": "ERROR"})
        before = self.evidence()
        output = self.cli("-b", "state_fixture", "explain", env=env, success=False)
        self.assertIn("Backend query failed", output)
        self.assertNotIn("Ordinary whole-workflow plan", output)
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
