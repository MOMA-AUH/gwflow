"""The public JSON status contract through the installed executable."""

import json
import os

from support import LocalBackendTestCase
import test_fresh
import test_inspection
import test_graphs
import test_status_notices
from test_registry_images import RegistryTestCase


class JsonStatusTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    snapshot = test_inspection.LifecycleInspectionTests.snapshot
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle
    append_workflow = test_status_notices.StatusNoticeTests.append_workflow

    def report(self, *options, backend=(), env=None):
        before = self.snapshot()
        result = self.cli_result(*backend, "status", "--json", *options, env=env)
        document = json.loads(result.stdout)
        self.assertEqual(set(document), {"schema_version", "tasks", "notices"})
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(self.snapshot(), before)
        return document

    def test_flat_summary_selection_and_unallocated_identity(self):
        document = self.report()
        self.assertEqual([task["name"] for task in document["tasks"]], ["a", "b", "c"])
        self.assertEqual(document["notices"], [])
        for task in document["tasks"]:
            self.assertEqual(set(task), {"name", "attempt_id", "state", "next_action", "progress", "reason"})
            self.assertIsNone(task["attempt_id"])
            self.assertEqual(task["state"], "pending")
            self.assertEqual(task["next_action"], "run")
            self.assertEqual(task["progress"], {"completed": 0, "total": 3})
            self.assertEqual(task["reason"]["code"], "no_attempt")
            self.assertIsInstance(task["reason"]["message"], str)
        for selectors, names in ((("a", "a", "b*"), ["a", "b"]), (("c",), ["c"]), (("*",), ["a", "b", "c"])):
            self.assertEqual([task["name"] for task in self.report(*selectors)["tasks"]], names)
        self.assertEqual(self.report("--status", "reusable")["tasks"], [])
        self.assertEqual(len(self.report("--status", "pending", "--status", "failed")["tasks"]), 3)

    def test_usage_errors_from_parser_and_callback_are_json(self):
        for options in (("--unknown",), ("--status", "nonsense"), ("--status",),
                        ("--plain",), ("--static",), ("missing",), ("a__compute",)):
            with self.subTest(options=options):
                result = self.cli_result("status", "--json", *options, success=False)
                self.assertEqual(result.returncode, 2)
                document = json.loads(result.stdout)
                self.assertEqual(set(document), {"schema_version", "error"})
                self.assertEqual(document["error"]["code"], "usage_error")
                self.assertIsInstance(document["error"]["message"], str)
                self.assertIn("Error:", result.stderr)
        self.assertEqual(self.report("--status", "cancelled")["tasks"], [])
        self.cli("status", "--plain", "--static")

    def test_workflow_and_child_output_use_stderr_and_loading_failure_has_no_rows(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                            "\nimport os, subprocess\nprint('workflow print')\n"
                            "os.write(1, b'workflow descriptor\\n')\n"
                            "subprocess.run(['printf', 'child print\\n'], check=True)\n")
        result = self.cli_result("status", "--json")
        self.assertEqual(len(json.loads(result.stdout)["tasks"]), 3)
        for message in ("workflow print", "workflow descriptor", "child print"):
            self.assertIn(message, result.stderr)
        workflow.write_text(workflow.read_text() + "\nraise RuntimeError('loading failed')\n")
        result = self.cli_result("status", "--json", success=False)
        self.assertEqual(result.returncode, 1)
        document = json.loads(result.stdout)
        self.assertEqual(set(document), {"schema_version", "error"})
        self.assertEqual(document["error"]["code"], "inspection_failed")
        self.assertIn("loading failed", document["error"]["message"])
        self.assertIn("child print", result.stderr)

    def test_terminal_json_is_one_shot(self):
        result = self.terminal_cli("status", "--json", width=100)
        self.assertEqual(len(json.loads(result.stdout)["tasks"]), 3)
        self.assertNotIn("\x1b", result.stdout)

    def test_late_inspection_failure_discards_prior_observations(self):
        self.run_complete()
        environment = self.inject(fail_status_prefix="c__compute")
        result = self.cli_result("-b", "recovery_fixture", "status", "--json", env=environment, success=False)
        self.assertEqual(result.returncode, 1)
        document = json.loads(result.stdout)
        self.assertEqual(set(document), {"schema_version", "error"})
        self.assertEqual(document["error"]["code"], "inspection_failed")

    def test_invalid_configuration_is_a_structured_inspection_failure(self):
        (self.work / ".gwfconf.json").write_text("{")
        result = self.cli_result("status", "--json", success=False)
        self.assertEqual(result.returncode, 1)
        document = json.loads(result.stdout)
        self.assertEqual(set(document), {"schema_version", "error"})
        self.assertEqual(document["error"]["code"], "inspection_failed")

    def test_filtered_notices_include_required_upstream_and_exclude_unrelated_blockers(self):
        self.run_complete()
        (self.work / "results/a/result.txt").unlink()
        self.append_workflow(
            "\nblocked = empty_task(inputs=[])\n"
            "target = blocked.target('compute', inputs=[], outputs=['out'], image='missing.sif')\n"
            "target << 'touch out'\n"
            "blocked.retain('out', source=target.output('out'), path='out')\n"
            "gwf.task(blocked, alias='unrelated')\n")
        selected = self.report("c", "--status", "reusable")
        self.assertEqual(selected["tasks"], [])
        self.assertEqual([(notice["code"], notice["task_names"]) for notice in selected["notices"]],
                         [("upstream_recovery_pending", ["c"])])
        all_tasks = self.report("--status", "queued")
        self.assertEqual(all_tasks["tasks"], [])
        notices = {notice["code"]: notice for notice in all_tasks["notices"]}
        self.assertEqual(notices["workflow_blocked"]["task_names"], ["unrelated"])
        self.assertEqual(notices["upstream_recovery_pending"]["task_names"], ["c"])
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "outputs=['private.txt'])", "outputs=['private.txt'], image='missing.sif')", 1))
        notices = {notice["code"]: notice for notice in self.report("c", "--status", "queued")["notices"]}
        self.assertEqual(notices["workflow_blocked"]["task_names"], ["a"])
        self.assertEqual(notices["upstream_recovery_pending"]["task_names"], ["c"])

    def test_completion_followup_only_for_retry(self):
        test_graphs.TaskGraphTests.configure_workflow(self, right_command="exit 8")
        self.cli("run")
        self.settle()
        self.assertEqual(self.report()["notices"], [])
        environment = self.inject(queued_prefix="sample__gwflow_complete")
        document = self.report("--status", "reusable", backend=("-b", "recovery_fixture"), env=environment)
        self.assertEqual(document["tasks"], [])
        self.assertEqual([(notice["code"], notice["task_names"]) for notice in document["notices"]],
                         [("completion_followup_required", ["sample"])])

    def test_ordinary_completion_activity_does_not_require_followup(self):
        self.run_complete()
        environment = self.inject(queued_prefix="b__gwflow_complete")
        document = self.report(backend=("-b", "recovery_fixture"), env=environment)
        self.assertEqual(document["notices"], [])

    def test_unresolved_submission_keeps_multiple_notices_and_recovery_limits(self):
        self.cli("-b", "recovery_fixture", "run", "a",
                 env=self.inject(reject_prefix="a__gwflow_prepare"), success=False)
        document = self.report("a", "--status", "reusable")
        self.assertEqual(document["tasks"], [])
        notices = {notice["code"]: notice for notice in document["notices"]}
        self.assertEqual(set(notices), {"workflow_blocked", "submission_investigation_required"})
        for notice in notices.values():
            self.assertEqual(notice["task_names"], ["a"])
        guidance = notices["submission_investigation_required"]["message"]
        self.assertIn("whole-Task inactivity", guidance)
        for kind in ("computation", "preparation", "completion"):
            self.assertIn(kind, guidance)


class JsonImageStatusTests(RegistryTestCase):
    def test_json_uses_scoped_upstream_acquisition_and_offline_warm_cache(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("gwf.task(task, alias='sample')", "sample = gwf.task(task, alias='sample')") +
                            "consumer = empty_task(inputs=[sample.outputs['result']])\n"
                            "target = consumer.target('copy', inputs=consumer.inputs, outputs=['copy'])\n"
                            "target << 'touch copy'\n"
                            "consumer.retain('result', source=target.output('copy'), path='copy')\n"
                            "gwf.task(consumer, alias='consumer')\n"
                            "unrelated = empty_task(inputs=[])\n"
                            "target = unrelated.target('compute', inputs=[], outputs=['out'], image='docker://other/image:tag')\n"
                            "target << 'touch out'\n"
                            "unrelated.retain('result', source=target.output('out'), path='out')\n"
                            "gwf.task(unrelated, alias='unrelated')\n")
        document = json.loads(self.cli_result("status", "--json", "consumer").stdout)
        self.assertEqual([task["name"] for task in document["tasks"]], ["consumer"])
        self.assertEqual(document["notices"], [])
        self.assertEqual([call["reference"] for call in self.calls("pull")], [self.reference])
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.assertEqual(self.calls("exec"), [])
        self.run_complete("sample")
        before = self.snapshot()
        self.options(fail=True)
        (self.bin / "apptainer").rename(self.bin / "hidden-apptainer")
        environment = {**os.environ, "PATH": str(self.bin)}
        document = json.loads(self.cli_result("status", "--json", "sample", env=environment).stdout)
        self.assertEqual(document["tasks"][0]["state"], "reusable")
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertEqual(self.snapshot(), before)



