"""Grouped status through installed CLI observations of declared Tasks."""

import re

from support import LocalBackendTestCase
import test_fresh
import test_graphs
import test_inspection


class GroupedStatusTests(LocalBackendTestCase):
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_graphs.TaskGraphTests.settle
    snapshot = test_inspection.LifecycleInspectionTests.snapshot
    def configure_workflow(self):
        (self.work / "library.py").write_text(
            "from gwflow import Task, shell, task_template\n"
            "@task_template\n"
            "def mapping(inputs=()):\n"
            "    task = Task(inputs=inputs)\n"
            "    target = task.target('compute', inputs=inputs, outputs=['out.txt'])\n"
            "    target << 'printf result > out.txt'\n"
            "    task.retain('result', source=target.output('out.txt'), path='out.txt')\n"
            "    return task\n"
        )
        (self.work / "workflow.py").write_text(
            "from gwflow import Workflow\n"
            "from library import mapping\n"
            "gwf = Workflow()\n"
            "a = gwf.task(mapping(), key='A')\n"
            "gwf.task(mapping(), alias='remapping', key='B')\n"
            "gwf.task(mapping(), alias='mapping', key='C')\n"
            "gwf.task(mapping())\n"
        )

    def test_default_groups_by_factory_and_effective_prefix(self):
        output = self.cli("status")
        self.assertIn("4 of 4 Tasks selected", output)
        self.assertIn("4 pending", output)
        self.assertRegex(output, r"Group\s+Reusable\s+Other states")
        self.assertRegex(output, r"(?m)^mapping\s+0/3\s+3 pending$")
        self.assertRegex(output, r"(?m)^remapping\s+0/1\s+1 pending$")
        self.assertNotIn("mapping__A", output)
        self.assertNotIn("Jobs completed", output)

    def test_explicit_views_preserve_whole_task_progress_and_details_win(self):
        for options in (("--instances",), ("--details",), ("--instances", "--details")):
            with self.subTest(options=options):
                output = self.cli("status", *options)
                self.assertIn("4 of 4 Tasks selected", output)
                for name in ("mapping__A", "remapping__B", "mapping__C", "mapping"):
                    self.assertRegex(output, rf"(?m)^Task {name}\s+pending\s+0/3")
                self.assertNotIn("Reusable", output)
                self.assertEqual(output.count("[preparation]"), 4 if "--details" in options else 0)
        output = self.cli("status", "mapping__A", "--instances")
        self.assertNotIn("[preparation]", output)
        self.assertIn("1 of 4 Tasks selected", output)

    def test_full_workflow_qualification_and_group_order_survive_filtering(self):
        (self.work / "second.py").write_text((self.work / "library.py").read_text())
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                            "from second import mapping as other\n"
                            "gwf.task(other(), key='D')\n")
        self.run_complete()
        environment = self.inject(job_states={"mapping__A__compute": "FAILED"})
        # Reuse is authoritative even if a backend reports stale failures.
        output = self.cli("-b", "recovery_fixture", "status", env=environment)
        self.assertRegex(output, r"(?m)^mapping@library.mapping\s+3/3$")
        self.assertRegex(output, r"(?m)^remapping\s+1/1$")
        self.assertRegex(output, r"(?m)^mapping@second.mapping\s+1/1$")
        (self.work / "results/mapping__C/out.txt").unlink()
        (self.work / "results/remapping__B/out.txt").unlink()
        output = self.cli("status", "--status", "repairable")
        self.assertIn("2 of 5 Tasks selected", output)
        self.assertRegex(output, r"(?m)^mapping@library.mapping\s+0/1\s+1 repairable$")
        self.assertRegex(output, r"(?m)^remapping\s+0/1\s+1 repairable$")
        self.assertLess(output.index("mapping@library.mapping"), output.index("remapping"))
        self.assertNotIn("mapping@second.mapping", output)
        empty = self.cli("status", "--status", "failed")
        self.assertIn("0 of 5 Tasks selected", empty)
        self.assertIn("No Tasks selected", empty)
        self.assertNotIn("mapping@", empty)

    def test_exact_active_phases_roll_up_only_in_overview(self):
        self.run_complete()
        for local, state in (("gwflow_prepare", "preparing"), ("compute", "running"),
                             ("gwflow_complete", "finishing")):
            with self.subTest(state=state):
                environment = self.inject(running_prefix="mapping__A__" + local, record_status=True)
                before = self.snapshot()
                record = self.work / "backend-observations.jsonl"
                baseline = None
                for options in ((), ("--instances",), ("--details",), ("--status", state)):
                    record.unlink(missing_ok=True)
                    output = self.cli("-b", "recovery_fixture", "status", *options, env=environment)
                    if "--instances" in options or "--details" in options:
                        self.assertRegex(output, rf"Task mapping__A\s+{state}\s+2/3")
                        self.assertNotIn("1 active", output)
                    else:
                        self.assertIn("1 active", output)
                        self.assertNotIn(state, output)
                    if baseline is None:
                        baseline = record.read_text().splitlines()
                    self.assertEqual(record.read_text().splitlines(), baseline)
                    self.assertEqual(self.snapshot(), before)
        environment = self.inject(queued_prefix="mapping__A__compute")
        output = self.cli("-b", "recovery_fixture", "status", env=environment)
        self.assertRegex(output, r"(?m)^mapping\s+2/3\s+1 queued$")
        self.assertNotIn("active", output)
        self.cli("status", "--status", "active", success=False)

    def test_repair_and_deferred_groups_keep_required_reminder(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                            "gwf.task(mapping([a.outputs['result']]), alias='consumer')\n")
        self.run_complete()
        (self.work / "results/mapping__A/out.txt").unlink()
        output = self.cli("status")
        self.assertRegex(output, r"(?m)^mapping\s+2/3\s+1 repairable$")
        self.assertRegex(output, r"(?m)^consumer\s+0/1\s+1 deferred$")
        self.assertIn("Task consumer: run again after upstream result recovery", output)

    def test_blocked_failed_and_canceled_each_count_one_primary_state(self):
        test_graphs.TaskGraphTests.configure_workflow(self, right_command="exit 8")
        self.cli("run")
        self.settle()
        environment = self.inject(running_prefix="sample__left")
        output = self.cli("-b", "recovery_fixture", "status", env=environment)
        self.assertRegex(output, r"(?m)^sample\s+0/1\s+1 failed$")
        self.assertNotIn("active", output)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "outputs=['same.txt'])", "outputs=['same.txt'], image='missing.sif')", 1))
        output = self.cli("status")
        self.assertRegex(output, r"(?m)^sample\s+0/1\s+1 blocked$")
        self.assertIn("Workflow blocked", output)
        output = self.cli("status", "--instances")
        self.assertRegex(output, r"Task sample\s+blocked\s+\?/5")
        # A separate rejected computation has no checked completion evidence.
        self.configure_workflow()
        self.cli("-b", "recovery_fixture", "run",
                 env=self.inject(reject_prefix="mapping__A__compute"), success=False)
        self.settle()
        environment = self.inject(job_states={"mapping__A__compute": "CANCELLED"})
        output = self.cli("-b", "recovery_fixture", "status", "--status", "canceled", env=environment)
        self.assertIn("1 of 4 Tasks selected", output)
        self.assertRegex(output, r"(?m)^mapping\s+0/1\s+1 canceled$")

    def test_forty_five_tasks_form_three_group_rows(self):
        (self.work / "workflow.py").write_text(
            "from gwflow import Workflow\n"
            "from library import mapping\n"
            "gwf = Workflow()\n"
            "gwf.task(mapping(), alias='prepare_reference')\n"
            "for index in range(24):\n"
            "    gwf.task(mapping(), alias='duplex_mapping', key=f'sample_{index:02}')\n"
            "for index in range(20):\n"
            "    gwf.task(mapping(), alias='mutect2_calling', key=f'sample_{index:02}')\n"
        )
        output = self.cli("status")
        self.assertIn("45 of 45 Tasks selected", output)
        self.assertEqual(re.findall(r"(?m)^(\S+)\s+0/\d+\s+\d+ pending$", output),
                         ["prepare_reference", "duplex_mapping", "mutect2_calling"])

    def test_endpoint_and_computation_group_filters_keep_the_overview(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                            "definition = mapping([a.outputs['result']])\n"
                            "definition.targets['compute'].group = 'report'\n"
                            "gwf.task(definition, alias='consumer')\n")
        output = self.cli("status", "--endpoints")
        self.assertIn("4 of 5 Tasks selected", output)
        self.assertRegex(output, r"(?m)^mapping\s+0/2\s+2 pending$")
        output = self.cli("status", "--endpoints", "--group", "report", "--status", "pending")
        self.assertIn("1 of 5 Tasks selected", output)
        self.assertRegex(output, r"(?m)^consumer\s+0/1\s+1 pending$")
        self.assertNotIn("Jobs completed", output)
