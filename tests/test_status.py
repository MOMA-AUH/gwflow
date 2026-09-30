"""Compact Task status through the ordinary gwf CLI."""

from gwf.backends.local import Client, LocalStatus

import test_reuse


class StatusCliTests(test_reuse.LocalBackendTestCase):
    def records(self):
        directory = self.work / ".gwf" / "gwflow"
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*.json")
        } if directory.exists() else {}

    def status_lines(self):
        return self.cli("status").splitlines()

    def test_pending_task_is_visible_before_any_job_finishes(self):
        lines = self.status_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("Task text", lines[0])
        self.assertIn("shouldrun", lines[0])
        self.assertIn("0/2 targets completed", lines[0])
        details = self.cli("status", "--details")
        self.assertIn("prepare", details)
        self.assertIn("finish", details)

    def test_submitted_task_expands_its_targets(self):
        (self.work / "middle.txt").write_text("hello\n")
        env = self.state_backend({
            "text__prepare": "COMPLETED",
            "text__finish": "SUBMITTED",
        })
        lines = self.cli("-b", "state_fixture", "status", env=env).splitlines()
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("Task text", lines[0])
        self.assertIn("submitted", lines[0])
        self.assertIn("1/2 targets completed", lines[0])
        self.assertIn("prepare", lines[1])
        self.assertIn("completed", lines[1])
        self.assertIn("finish", lines[2])
        self.assertIn("submitted", lines[2])

    def test_reused_task_collapses_inner_targets_without_writing_records(self):
        for tracking in (False, True):
            with self.subTest(tracking=tracking):
                self.configure(use_spec_hashes=tracking)
                self.run_complete()
                (self.work / "middle.txt").unlink()
                before = self.records()
                lines = self.status_lines()
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("Task text", lines[0])
                self.assertIn("reusable", lines[0])
                self.assertIn("2 targets omitted by reuse", lines[0])
                details = self.cli("status", "--details")
                self.assertIn("prepare", details)
                self.assertIn("omitted by reuse", details)
                self.assertIn("completion", details)
                self.assertEqual(self.records(), before)
                self.assertFalse((self.work / "middle.txt").exists())
                self.assertEqual(self.cli("status", "--status", "failed").strip(), "")
                self.assertIn("text__gwflow_complete", self.cli("status", "--format", "default"))
                self.assertIn("completed", self.cli("status", "--format", "summary"))

    def test_mixed_reused_and_ordinary_tasks_keep_active_and_failed_targets(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        (self.work / "middle.txt").unlink()
        with (self.work / "workflow.py").open("a") as stream:
            stream.write(
                "from gwflow import Task\n"
                "busy = Task(inputs=['input.txt'], outputs=['busy.txt'])\n"
                "busy.target('wait', inputs=['input.txt'], outputs=['busy.txt']) << "
                "'touch busy.started; while [ ! -f busy.release ]; do sleep 0.05; done; cp input.txt busy.txt'\n"
                "gwf.task_from_template('busy', busy)\n"
                "broken = Task(inputs=['input.txt'], outputs=['broken.txt'])\n"
                "broken.target('fail', inputs=['input.txt'], outputs=['broken.txt']) << 'exit 1'\n"
                "gwf.task_from_template('broken', broken)\n"
            )
        def release_busy():
            (self.work / "busy.release").touch()

            def no_running_jobs():
                with Client.connect(port=self.port) as client:
                    return LocalStatus.RUNNING not in client.status().values()

            self.wait_for(no_running_jobs)

        self.addCleanup(release_busy)
        self.cli("run")

        def active_and_failed():
            with Client.connect(port=self.port) as client:
                states = set(client.status().values())
            return LocalStatus.RUNNING in states and LocalStatus.FAILED in states

        self.wait_for(active_and_failed)
        before = self.records()
        lines = self.status_lines()
        self.assertEqual(len(lines), 5, lines)
        self.assertTrue(any("Task text" in line and "reusable" in line for line in lines))
        self.assertTrue(any("Task busy" in line and "running" in line for line in lines))
        self.assertTrue(any("wait" in line and "running" in line for line in lines))
        self.assertTrue(any("Task broken" in line and "failed" in line for line in lines))
        self.assertTrue(any("fail" in line and "failed" in line for line in lines))
        failed = self.cli("status", "--status", "failed")
        self.assertIn("Task broken", failed)
        self.assertNotIn("Task text", failed)
        self.assertNotIn("Task busy", failed)
        self.assertEqual(self.records(), before)
        self.assertFalse((self.work / "middle.txt").exists())

    def test_empty_workflow_has_empty_default_status(self):
        (self.work / "workflow.py").write_text("from gwflow import Workflow\ngwf = Workflow()\n")
        self.assertEqual(self.cli("status").strip(), "")

    def test_ordinary_target_is_shown_outside_task_tree(self):
        with (self.work / "workflow.py").open("a") as stream:
            stream.write("gwf.target('plain', inputs=[], outputs=['plain.txt']) << 'touch plain.txt'\n")
        output = self.cli("status")
        self.assertIn("Task text", output)
        self.assertIn("Target plain", output)
        self.assertIn("shouldrun", output)
        self.assertIn("Target plain", self.cli("status", "plain"))
        self.assertNotIn("Task text", self.cli("status", "plain"))
        self.assertIn("Task text", self.cli("status", "--endpoints"))
        self.assertIn("Target plain", self.cli("status", "--group", "none"))

    def test_plain_gwf_workflow_keeps_original_status_output(self):
        (self.work / "workflow.py").write_text(
            "from gwf import Workflow\n"
            "gwf = Workflow()\n"
            "gwf.target('plain', inputs=[], outputs=['plain.txt']) << 'touch plain.txt'\n"
        )
        output = self.cli("status")
        self.assertIn("plain", output)
        self.assertIn("(id: none)", output)
        self.assertNotIn("Target plain", output)
