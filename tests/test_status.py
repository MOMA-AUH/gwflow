"""Readable managed status through the installed CLI and local backend."""

import errno
import os
import pty
import shlex
import subprocess

from support import GWF, LocalBackendTestCase
import test_fresh
import test_graphs


class ManagedStatusTests(LocalBackendTestCase):
    workers = 1
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle

    def test_default_is_compact_and_task_selection_expands_local_names(self):
        output = self.cli("status")
        self.assertRegex(output, r"(?m)^\. Task sample\s+pending\s+0/3 targets completed$")
        self.assertNotIn("next:", output)
        self.assertNotIn("fresh attempt", output)
        selected = self.cli("status", "sample")
        self.assertRegex(selected, r"(?m)^  \|-- \. left\s+pending$")
        self.assertRegex(selected, r"(?m)^  `-- \. join\s+pending$")
        self.assertNotIn("gwflow_prepare", selected)
        self.assertNotIn("Attempt:", selected)
        self.assertRegex(self.cli("status", "sample__gwflow_prepare"), r"preparation\s+pending")

    def test_failed_task_expands_and_matches_failed_filter_before_retry(self):
        self.configure_workflow(right_command="exit 8")
        self.cli("run")
        self.settle()
        output = self.cli("status", "--status", "failed")
        self.assertRegex(output, r"(?m)^! Task sample\s+failed\s+.*retry available")
        self.assertRegex(output, r"(?m)^  .* ! right\s+failed$")
        self.assertNotIn("Attempt:", output)
        self.assertNotIn("__gwflow_", output)
        failed_targets = self.cli("status", "--status", "failed", "--format", "default")
        self.assertIn("sample__right", failed_targets)
        self.assertNotIn("sample__left", failed_targets)
        details = self.cli("status", "--details")
        self.assertIn("Next: retry", details)
        for label in ("Reason:", "Attempt:", "Workspace:", "Backend job:", "Log stderr:"):
            self.assertIn(label, details)

    def test_active_targets_distinguish_running_and_submitted(self):
        held, release = self.work / "held", self.work / "release"
        self.configure_workflow(left_command=(
            f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; "
            "do sleep 0.025; done; printf left > same.txt"
        ))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            output = self.cli("status")
            self.assertRegex(output, r"Task sample\s+running")
            self.assertRegex(output, r"left\s+running")
            self.assertRegex(output, r"right\s+submitted")
            selected = self.cli("status", "sample__right", "--status", "submitted")
            self.assertRegex(selected, r"right\s+submitted")
            self.assertNotIn("left", selected)
            self.assertNotIn("join", selected)
            self.assertNotIn("sample", self.cli("status", "--status", "failed"))
        finally:
            release.touch()
        self.finish()

    def terminal_status(self, *options):
        master, slave = pty.openpty()
        try:
            result = subprocess.run([GWF, *options, "status"], cwd=self.work,
                                    stdout=slave, stderr=slave, timeout=30)
            os.close(slave)
            slave = None
            output = b""
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output += chunk
            self.assertEqual(result.returncode, 0, output.decode())
            return output.decode()
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)

    def test_terminal_colors_and_readable_piped_and_no_color_output(self):
        self.assertIn("\x1b[35m. Task sample", self.terminal_status("--use-color"))
        self.assertNotIn("\x1b[", self.cli("status"))
        self.assertNotIn("\x1b[", self.terminal_status("--no-color"))
        self.run_complete()
        self.assertIn("\x1b[32m+ Task sample", self.terminal_status("--use-color"))

    def test_formats_group_patterns_and_empty_selections(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "gwf.task_from_template", "left.group = 'mapping'\nright.group = 'mapping'\ngwf.task_from_template"))
        flat = self.cli("status", "--format", "default")
        self.assertIn("sample__left", flat)
        self.assertNotIn("Task sample", flat)
        self.assertNotIn("gwflow_prepare", flat)
        self.assertRegex(self.cli("status", "--format", "summary"), r"shouldrun\s+3")
        self.assertRegex(self.cli("status", "--format", "grouped", "--group", "map*"), r"mapping\s+2 shouldrun")
        grouped = self.cli("status", "--group", "map*")
        self.assertIn("left", grouped)
        self.assertIn("right", grouped)
        self.assertNotIn("join", grouped)
        for output_format in ("tree", "default", "summary", "grouped"):
            self.assertEqual(self.cli("status", "--format", output_format, "absent*"), "")
        self.assertIn("--details requires --format tree", self.cli(
            "status", "--details", "--format", "summary", success=False))

    def test_reuse_after_cleanup_does_not_display_failed_or_pending_targets(self):
        self.run_complete()
        self.cli("clean-work", "--delete")
        output = self.cli("status")
        self.assertRegex(output, r"Task sample\s+reusable\s+work-cleaned")
        self.assertNotIn("left", output)
        expanded = self.cli("status", "sample")
        self.assertRegex(expanded, r"left\s+completed")
        self.assertNotIn("failed", expanded)
        self.assertRegex(self.cli("status", "--format", "summary"), r"completed\s+3")

    def test_endpoints_use_task_dependencies_and_fresh_work_hides_old_completion(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        output = self.cli("status", "--endpoints")
        self.assertNotIn("Task a", output)
        self.assertIn("Task b", output)
        self.assertIn("Task c", output)
        self.assertNotIn("a__compute", self.cli("status", "--endpoints", "--format", "default"))
        self.configure(use_spec_hashes=True)
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a", "printf changed"))
        output = self.cli("status", "a")
        self.assertRegex(output, r"Task a\s+pending\s+0/1 target completed; fresh computation required")
        self.assertRegex(output, r"compute\s+pending")
        self.assertNotIn("reusable", output)
