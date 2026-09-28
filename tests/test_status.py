"""Default status projection through the ordinary gwf CLI."""

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
        return [line for line in self.cli("status").splitlines() if " (id: " in line]

    def test_reused_task_projects_one_completed_entry_without_writing_records(self):
        for tracking in (False, True):
            with self.subTest(tracking=tracking):
                self.configure(use_spec_hashes=tracking)
                self.run_complete()
                (self.work / "middle.txt").unlink()
                before = self.records()
                lines = self.status_lines()
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("text__gwflow_complete", lines[0])
                self.assertIn("completed", lines[0])
                self.assertEqual(self.records(), before)
                self.assertFalse((self.work / "middle.txt").exists())
                self.assertEqual(self.cli("status", "--status", "failed").strip(), "")

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
        self.addCleanup((self.work / "busy.release").touch)
        self.cli("run")

        def active_and_failed():
            with Client.connect(port=self.port) as client:
                states = set(client.status().values())
            return LocalStatus.RUNNING in states and LocalStatus.FAILED in states

        self.wait_for(active_and_failed)
        before = self.records()
        lines = self.status_lines()
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(any("text__gwflow_complete" in line and "completed" in line for line in lines))
        self.assertTrue(any("busy__wait" in line and "running" in line for line in lines))
        self.assertTrue(any("broken__fail" in line and "failed" in line for line in lines))
        self.assertEqual(self.records(), before)
        self.assertFalse((self.work / "middle.txt").exists())

    def test_empty_workflow_has_empty_default_status(self):
        (self.work / "workflow.py").write_text("from gwflow import Workflow\ngwf = Workflow()\n")
        self.assertEqual(self.cli("status").strip(), "")
