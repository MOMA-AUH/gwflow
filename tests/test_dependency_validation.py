"""Bound dependency observations through completed, installed CLI workflows."""

import time

from record_fixtures import record_path, read_record
from support import LocalBackendTestCase, TASK_FACTORY
import test_planning_io


class DependencyValidationTests(LocalBackendTestCase):
    workers = 4
    probe = test_planning_io.PlanningObservationTests.probe
    scenario = test_planning_io.PlanningObservationTests.scenario

    def wait_for(self, predicate):
        # These dependency workloads prepare dozens of real lifecycle jobs;
        # the regression itself uses operation counts, never elapsed time.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Timed out preparing the dependency validation workload")

    def configure_workflow(self, count=8, width=2):
        (self.work / "workflow.py").write_text(
            TASK_FACTORY + "from gwflow import Workflow, shell\n"
            "gwf = Workflow()\n"
            "handles = []\n"
            f"for index in range({count}):\n"
            f"    incoming = [handle.outputs['value'] for handle in handles[-{width}:]] or ['input.txt']\n"
            "    task = empty_task(inputs=incoming)\n"
            "    target = task.target('compute', inputs=incoming, outputs=['value.txt'])\n"
            "    target << shell('printf value > value.txt')\n"
            "    task.retain('value', source=target.output('value.txt'), path='value.txt')\n"
            "    handles.append(gwf.task(task, alias=f'node{index:02}'))\n"
        )

    def test_shared_ancestors_have_bounded_validation_and_survive_cleanup(self):
        self.run_complete()
        for cleaned in (False, True):
            with self.subTest(cleaned=cleaned):
                if cleaned:
                    self.cli("clean", "--delete")
                output, report = self.probe("run", "--dry-run", "node07")
                self.assertEqual(output.count("Reuse"), 8)
                self.assertNoSubmissions(output)
                # Each selected Task can observe every ancestor, but never
                # once per dependency path through the repeated diamonds.
                self.assertLessEqual(report["counts"]["planning"]["input_validations"], 2 * 8 * 8)
                self.assertLessEqual(report["peak_cached_documents"], 8)
                self.assertLessEqual(report["peak_opened_descriptors"], 67)
                self.assertIn("No new jobs were submitted", self.cli("run", "node07"))

    def test_deep_chain_has_bounded_validation_and_call_depth(self):
        self.configure_workflow(count=16, width=1)
        self.run_complete()
        output, report = self.probe("run", "--dry-run", "node15")
        self.assertEqual(output.count("Reuse"), 16)
        self.assertNoSubmissions(output)
        self.assertLessEqual(report["counts"]["planning"]["input_validations"], 2 * 16 * 16)
        self.assertLessEqual(report["peak_producer_depth"], 4)
        self.assertLessEqual(report["peak_cached_documents"], 8)
        self.assertLessEqual(report["peak_opened_descriptors"], 67)

    def test_changed_shared_ancestor_record_is_rechecked_after_prior_validation(self):
        self.configure_workflow(count=4)
        self.run_complete()
        completion = record_path(self.work, "node00", "operations/*/completion.json")
        original = read_record(completion)
        consumer = record_path(self.work, "node02", "attempt.json")
        first, second = self.scenario([["run", "--dry-run"], ["run", "--dry-run"]], [
            {"pass": 0, "after": str(consumer),
             "write": {str(completion): {**original, "attempt": "0" * 32}}},
            {"pass": 0, "after": "planned", "write": {str(completion): original}},
        ])
        self.assertNotEqual(first["exit_code"], 0)
        self.assertRegex(first["output"], r"Task node00\s+Reuse\s+")
        self.assertNotRegex(first["output"], r"Task node03\s+Reuse\s+")
        self.assertIn("lacks checked Completion", first["output"])
        self.assertNoSubmissions(first["output"])
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual(second["output"].count("Reuse"), 4)
        self.assertIn("No new jobs were submitted", self.cli("run"))
