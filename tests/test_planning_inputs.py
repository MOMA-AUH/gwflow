"""Shared external-input observations through public authoring and the CLI."""

import shutil

import test_planning_io
from test_registry_images import RegistryTestCase

from support import TASK_FACTORY


class SharedInputObservationTests(RegistryTestCase):
    probe = test_planning_io.PlanningObservationTests.probe
    scenario = test_planning_io.PlanningObservationTests.scenario

    def configure_workflow(self, tasks=1, targets=1, reference=None):
        RegistryTestCase.configure_workflow(self, reference)
        reference = self.reference if reference is None else reference
        source = TASK_FACTORY + "from gwflow import Task, Workflow, shell\ngwf = Workflow()\n"
        for index in range(tasks):
            source += "task = empty_task(inputs=['input.txt'])\n"
            for local in range(targets):
                source += (
                    f"target = task.target('compute{local}', inputs=task.inputs, outputs=['out{local}.txt'], image={reference!r})\n"
                    f"target << shell('cat {{source}} > out{local}.txt', source='input.txt')\n"
                    f"task.retain('result{local}', source=target.output('out{local}.txt'), path='out{local}.txt')\n"
                )
            source += f"gwf.task(task, alias='sample{index}')\n"
        (self.work / "workflow.py").write_text(source)
        shutil.rmtree(self.work / "__pycache__", ignore_errors=True)
        self.evidence_label = f"tasks-{tasks}-targets-{targets}-"

    def test_shared_input_and_image_inspection_stays_bounded(self):
        self.cli("status")
        image, = self.cached_images()
        for tasks, targets in ((1, 1), (4, 1), (1, 4)):
            with self.subTest(tasks=tasks, targets=targets):
                self.configure_workflow(tasks, targets)
                output, report = self.probe("status")
                self.assertEqual(output, self.cli_result("status").stdout)
                self.assertNotIn("blocked", output)
                paths = report["paths"]["planning"]
                self.assertEqual(paths["stat:" + str(self.work / "input.txt")], 1)
                self.assertEqual(paths["stat:" + str(image)], 1)
                self.assertEqual(paths["access:" + str(image)], 1)
        self.assertEqual(len(self.calls("pull")), 1)

    def test_publication_after_missing_input_and_acquisition_refreshes_same_process(self):
        self.cli("status")
        image, = self.cached_images()
        self.configure_workflow(tasks=4)
        image.unlink()
        (self.work / "input.txt").unlink()
        first, second = self.scenario(
            [["status", "--details"], ["status", "--details"]],
            [{"pass": 0, "after": "planned", "write": {str(self.work / "input.txt"): "published"}}],
        )
        self.assertIn("Cannot observe external input", first["output"])
        self.assertNotIn("blocked", second["output"].lower())
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(len(self.cached_images()), 1)

    def test_post_planning_input_change_prevents_repair(self):
        self.run_complete()
        (self.work / "results/sample0/out0.txt").unlink()
        first, second = self.scenario(
            [["run"], ["explain", "--details"]],
            [{"pass": 0, "after": "planned", "write": {str(self.work / "input.txt"): "changed after planning"}}],
        )
        self.assertNotEqual(first["exit_code"], 0, first["output"])
        self.assertIn("input", first["output"])
        self.assertIn("fresh attempt", second["output"])
        self.assertFalse((self.work / "results/sample0/out0.txt").exists())

    def test_same_process_revalidates_alias_and_managed_locations(self):
        self.cli("status")
        image, = self.cached_images()
        alias = self.work / "alias.sif"
        alias.symlink_to(image)
        self.configure_workflow(reference=str(alias))
        other = self.work / "work" / "image.sif"
        other.parent.mkdir()
        other.write_bytes(image.read_bytes())
        first, second = self.scenario(
            [["status", "--details"], ["status", "--details"]],
            [{"pass": 0, "after": "planned", "remove": [str(alias)], "symlink": {str(alias): str(other)}}],
        )
        self.assertNotIn("blocked", first["output"].lower())
        self.assertRegex(second["output"], r"Task sample0\s+blocked")
        self.assertIn("External input points into managed storage", self.cli("explain", "--details"))

    def test_missing_local_image_does_not_poison_later_acquisition_in_same_pass(self):
        self.cli("status")
        image, = self.cached_images()
        image.unlink()
        self.configure_workflow(tasks=2)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(repr(self.reference), repr(str(image)), 1))
        first, second = self.scenario([["status", "--details"], ["status", "--details"]], [])
        self.assertRegex(first["output"], r"Task sample0\s+blocked")
        self.assertRegex(first["output"], r"Task sample1\s+pending\s+0/3")
        self.assertNotIn("Blocked", second["output"])
        self.assertEqual(len(self.calls("pull")), 2)

    def test_completed_shared_images_preserve_all_planning_commands(self):
        self.run_complete()
        image, = self.cached_images()
        for command in (("status", "--details"), ("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                output, report = self.probe(*command)
                self.assertEqual(output, self.cli_result(*command).stdout)
                paths = report["paths"]["planning"]
                self.assertEqual(paths["stat:" + str(self.work / "input.txt")], 1)
                self.assertEqual(paths["stat:" + str(image)], 1)
                self.assertEqual(paths["access:" + str(image)], 1)
        self.assertEqual(len(self.calls("pull")), 1)

    def test_reacquired_image_replaces_prior_positive_observation_in_same_pass(self):
        self.cli("status")
        image, = self.cached_images()
        self.configure_workflow(tasks=2)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(repr(self.reference), repr(str(image)), 1))
        self.run_complete()
        self.options(content="changed image acquired during planning\n")
        first, second = self.scenario(
            [["explain", "--details"], ["explain", "--details"]],
            [{"pass": 0, "after": "image:" + str(image), "remove": [str(image)]}],
        )
        sample = first["output"].split("Details for Task sample1:")[1]
        self.assertIn("input metadata changed", sample)
        self.assertIn("fresh attempt", sample)
        self.assertIn("input metadata changed", second["output"])
        self.assertEqual(len(self.calls("pull")), 2)
