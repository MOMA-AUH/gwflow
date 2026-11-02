"""New Task input blockers remain visible through installed status and run."""

import os

from support import LocalBackendTestCase, TASK_FACTORY
import test_json_status


class NewTaskInputTests(LocalBackendTestCase):
    report = test_json_status.JsonStatusTests.report
    snapshot = test_json_status.JsonStatusTests.snapshot

    def configure_workflow(self, source="missing.txt"):
        declaration = TASK_FACTORY + "from gwflow import Workflow, shell\ngwf = Workflow()\n"
        for name, path in (("unavailable", source), ("healthy", "input.txt")):
            declaration += (
                f"task = empty_task(inputs=[{path!r}])\n"
                "target = task.target('copy', inputs=task.inputs, outputs=['out.txt'])\n"
                f"target << shell('cat {{source}} > out.txt', source={path!r})\n"
                "task.retain('result', source=target.output('out.txt'), path='result.txt')\n"
                f"gwf.task(task, alias={name!r})\n"
            )
        (self.work / "workflow.py").write_text(declaration)

    def test_missing_input_reports_both_new_tasks_without_allocating_attempts(self):
        document = self.report()
        rows = {row["name"]: row for row in document["tasks"]}
        self.assertEqual(set(rows), {"unavailable", "healthy"})
        blocked = rows["unavailable"]
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["next_action"], "blocked")
        self.assertEqual(blocked["reason"]["code"], "input_unavailable")
        self.assertIn("missing.txt", blocked["reason"]["message"])
        self.assertEqual(rows["healthy"]["state"], "pending")
        self.assertEqual(rows["healthy"]["next_action"], "run")
        self.assertEqual(rows["healthy"]["reason"]["code"], "no_attempt")
        for row in rows.values():
            self.assertIsNone(row["attempt_id"])
            self.assertEqual(row["progress"], {"completed": 0, "total": 3})
        self.assertEqual([(notice["code"], notice["task_names"]) for notice in document["notices"]],
                         [("workflow_blocked", ["unavailable"])])
        for root in (".gwf/gwflow", "work", "results"):
            self.assertFalse((self.work / root).exists())

    def test_text_status_and_filtered_notices_preserve_the_blocker(self):
        before = self.snapshot()
        for mode in ("--static", "--plain"):
            with self.subTest(mode=mode):
                output = self.cli("status", "*", mode)
                self.assertRegex(output, r"(?m)^unavailable\s+blocked\s+0/3")
                self.assertRegex(output, r"(?m)^healthy\s+pending\s+0/3")
                self.assertIn("no new jobs will be submitted", output)
                filtered = self.cli("status", "*", mode, "--status", "pending")
                self.assertRegex(filtered, r"(?m)^healthy\s+pending\s+0/3")
                self.assertNotRegex(filtered, r"(?m)^unavailable\s+blocked")
                self.assertIn("unavailable (outside selection)", filtered)
        for state, names in (("pending", ["healthy"]), ("reusable", [])):
            document = self.report("--status", state)
            self.assertEqual([row["name"] for row in document["tasks"]], names)
            self.assertEqual([(notice["code"], notice["task_names"]) for notice in document["notices"]],
                             [("workflow_blocked", ["unavailable"])])
        self.assertEqual(self.snapshot(), before)

    def test_healthy_selection_can_inspect_and_run_without_the_unrelated_blocker(self):
        for mode in ("--static", "--plain"):
            output = self.cli("status", "healthy", mode)
            self.assertRegex(output, r"(?m)^healthy\s+pending\s+0/3")
            self.assertNotIn("unavailable", output)
            self.assertNotIn("Workflow notices", output)
        document = self.report("healthy", "--details")
        self.assertEqual([row["name"] for row in document["tasks"]], ["healthy"])
        self.assertEqual(document["notices"], [])
        preview = self.cli("run", "healthy", "--dry-run")
        self.assertRegex(preview, r"Task healthy\s+Run\s+")
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.cli("run", "healthy")
        self.cli("wait", "--interval", "0.1", "healthy")
        self.assertEqual((self.work / "results/healthy/result.txt").read_text(), "hello\n")
        rows = {row["name"]: row for row in self.report()["tasks"]}
        self.assertIsNone(rows["unavailable"]["attempt_id"])
        self.assertEqual(rows["healthy"]["state"], "reusable")
        self.assertFalse((self.work / "results/unavailable").exists())

    def test_blocked_execution_scope_rejects_preview_and_run_before_submission(self):
        before = self.snapshot()
        for options in (("--dry-run",), ()):
            output = self.cli("run", *options, success=False)
            self.assertIn("missing.txt", output)
            self.assertNoSubmissions(output)
            self.assertEqual(self.snapshot(), before)
            for root in (".gwf/gwflow", "work", "results"):
                self.assertFalse((self.work / root).exists())

    def test_nonregular_and_uncheckable_inputs_are_task_blockers(self):
        (self.work / "directory").mkdir()
        os.mkfifo(self.work / "pipe")
        (self.work / "loop").symlink_to("loop")
        for source in ("directory", "pipe", "loop"):
            with self.subTest(source=source):
                self.configure_workflow(source)
                rows = {row["name"]: row for row in self.report()["tasks"]}
                blocked = rows["unavailable"]
                self.assertEqual(blocked["state"], "blocked")
                self.assertEqual(blocked["next_action"], "blocked")
                self.assertEqual(blocked["reason"]["code"], "input_unavailable")
                self.assertIn(source, blocked["reason"]["message"])
                self.assertIsNone(blocked["attempt_id"])
                self.assertEqual(rows["healthy"]["state"], "pending")

    def test_managed_storage_input_keeps_its_validation_reason(self):
        (self.work / "work").mkdir()
        source = self.work / "work/managed.txt"
        source.write_text("untouched\n")
        self.configure_workflow(str(source))
        rows = {row["name"]: row for row in self.report()["tasks"]}
        self.assertEqual(rows["unavailable"]["next_action"], "blocked")
        self.assertEqual(rows["unavailable"]["reason"]["code"], "validation_failed")
        self.assertIn("managed storage", rows["unavailable"]["reason"]["message"])
        self.assertEqual(rows["healthy"]["state"], "pending")
        self.assertIn("managed storage", self.cli("run", success=False))
        self.assertEqual(source.read_text(), "untouched\n")
