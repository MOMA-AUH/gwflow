"""Ownership comparison bounds through the Store validation boundary."""

import json
import shutil
import sys
import tempfile
import unittest
from itertools import permutations

from gwf.exceptions import WorkflowError
from gwflow.lifecycle import Store

from record_fixtures import read_record, record_path, write_record
from support import FIXTURES, TASK_FACTORY, LocalBackendTestCase
import test_planning_io
import test_storage


class OwnershipComparisons:
    """Observe real comparisons without replacing ownership or record code."""

    def __init__(self):
        self.count = 0

    def profile(self, frame, event, arg):
        if (event == "call" and frame.f_code.co_name == "_overlap"
                and frame.f_back.f_code.co_name == "validate_result_ownership"):
            self.count += 1

    def __enter__(self):
        self.previous = sys.getprofile()
        sys.setprofile(self.profile)
        return self

    def __exit__(self, *error):
        sys.setprofile(self.previous)


class ResultOwnershipScalingTests(unittest.TestCase):
    def test_complete_inventory_has_a_linear_overlap_comparison_bound(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            for count in (64, 128, 256):
                with self.subTest(tasks=count):
                    # Reverse insertion order and common parents require the
                    # whole inventory, including omitted recorded Tasks.
                    recorded = [{"task": f"task{i}", "result_dir": f"samples/{i}/report"}
                                for i in reversed(range(count))]
                    with OwnershipComparisons() as comparisons:
                        store.validate_result_ownership({}, attempts=recorded)
                    self.assertGreater(comparisons.count, 0)
                    self.assertLessEqual(comparisons.count, count)

    def test_component_order_detects_conflicts_in_every_declaration_order(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            for destination in ("a", "a/child", "a/child/deep"):
                # A raw string sort places a- between a and a/child.
                for items in permutations((("owner", "a"), ("sibling", "a-"), ("new", destination))):
                    with self.subTest(items=items):
                        with self.assertRaisesRegex(WorkflowError, "ownership conflict") as error:
                            store.validate_result_ownership(dict(items), attempts=[])
                        self.assertIn("'owner'", str(error.exception))
                        self.assertIn("'new'", str(error.exception))

    def test_sibling_prefixes_and_empty_inventories_are_disjoint(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            for destinations in ({}, {"only": "a"}, {"a": "a-", "b": "a/child", "c": "a/children"}):
                store.validate_result_ownership(destinations, attempts=[])

    def test_declared_owner_matches_recorded_location_without_hiding_an_overlap(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            recorded = [{"task": "owner", "result_dir": "a"}]
            store.validate_result_ownership({"owner": "a", "sibling": "a-"}, attempts=recorded)
            with self.assertRaisesRegex(WorkflowError, "ownership conflict"):
                store.validate_result_ownership({"owner": "a", "sibling": "a-", "new": "a/child"}, attempts=recorded)
            with self.assertRaisesRegex(WorkflowError, "Recorded Task results location changed"):
                store.validate_result_ownership({"owner": "moved"}, attempts=recorded)


class OwnershipCommandScalingTests(LocalBackendTestCase):
    probe = test_planning_io.PlanningObservationTests.probe

    def configure_workflow(self, count=8):
        (self.work / "workflow.py").write_text(
            TASK_FACTORY + "from gwflow import Workflow\ngwf = Workflow()\n"
            "task = empty_task(inputs=[])\n"
            "target = task.target('compute', inputs=[], outputs=['value'])\n"
            "target << 'printf value > value'\n"
            "task.retain('value', source=target.output('value'), path='value')\n"
            f"for index in reversed(range({count})):\n"
            "    gwf.task(task, alias=f'task{index}', result_dir=f'samples/{index}/report')\n"
        )
        self.evidence_label = f"ownership-{count}-"

    def test_initialization_and_planning_comparison_bounds_as_inventory_grows(self):
        for count in (8, 16, 32):
            with self.subTest(tasks=count):
                self.configure_workflow(count)
                _, report = self.probe("run")
                comparisons = sum(phase.get("ownership_comparisons", 0) for phase in report["counts"].values())
                self.assertGreater(comparisons, 0)
                # Each initialization reads the current complete inventory.
                # Its two mutation checks must have aggregate quadratic,
                # rather than cubic, overlap-comparison cost.
                self.assertLessEqual(comparisons, 2 * count * count)
                self.finish()
                for command in (("status",), ("run", "--dry-run")):
                    _, report = self.probe(*command)
                    comparisons = sum(phase.get("ownership_comparisons", 0) for phase in report["counts"].values())
                    self.assertGreater(comparisons, 0)
                    self.assertLessEqual(comparisons, count)
                self.assertEqual((self.work / f"results/samples/{count - 1}/report/value").read_text(), "value")


class OwnershipMutationTests(LocalBackendTestCase):
    configure_workflow = test_storage.StoragePlacementTests.configure_workflow
    inject = test_storage.StoragePlacementTests.inject
    settle = test_storage.StoragePlacementTests.settle
    scenario = test_planning_io.PlanningObservationTests.scenario

    def omitted_owner(self):
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        workflow.write_text(original + "gwf.task(task, alias='b', result_dir='samples/b/report')\n")
        self.run_complete()
        self.cli("clean", "b", "--delete")
        workflow.write_text(original)
        path = record_path(self.work, "b", "attempt.json")
        return path, read_record(path)

    def result_snapshot(self):
        return {str(path.relative_to(self.work)): (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                for path in (self.work / "results").rglob("*") if path.is_file()}

    def test_initialization_rereads_omitted_ownership_after_planning(self):
        self.check_after_planning(repair=False)

    def test_repair_selection_rereads_omitted_ownership_after_planning(self):
        self.check_after_planning(repair=True)

    def check_after_planning(self, *, repair):
        path, original = self.omitted_owner()
        if repair:
            (self.work / "results/samples/a/report/renamed.txt").unlink()
        selected = record_path(self.work, "a", "attempt.json")
        before = read_record(selected), self.result_snapshot(), (self.work / "trace").read_bytes()
        for changed, diagnostic in (({**original, "result_dir": "samples/a/report/child"}, "ownership conflict"),
                                    ({}, "Malformed selected attempt")):
            with self.subTest(diagnostic=diagnostic):
                result, = self.scenario([["run", "a", *([] if repair else ["--force"])]], [
                    {"pass": 0, "after": "planned", "write": {str(path): changed}},
                ])
                self.assertNotEqual(result["exit_code"], 0, result)
                self.assertIn(diagnostic, result["output"])
                self.assertNoSubmissions(result["output"])
                self.assertEqual((read_record(selected), self.result_snapshot(), (self.work / "trace").read_bytes()), before)
                write_record(path, original)

    def test_completion_rereads_omitted_ownership_before_installation(self):
        self.check_completion(malformed=False)

    def test_completion_reports_current_malformed_omitted_evidence(self):
        self.check_completion(malformed=True)

    def check_completion(self, *, malformed):
        path, original = self.omitted_owner()
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        before = self.result_snapshot()
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_after_manifest": True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="a__gwflow_complete"))
        self.wait_for((self.work / "manifest-held").exists)
        selected = record_path(self.work, "a", "attempt.json")
        attempt = read_record(selected)
        try:
            write_record(path, {} if malformed else {**original, "result_dir": "samples/a/report/child"})
        finally:
            (self.work / "manifest-release").touch()
        self.settle()
        self.assertEqual(self.result_snapshot(), before)
        self.assertEqual(read_record(selected), attempt)
        # The log command also validates the inventory. Restore the omitted
        # record before reading the worker's preserved failure diagnostic.
        write_record(path, original)
        output = self.cli("logs", "a__gwflow_complete", "--stderr", "--no-pager")
        self.assertIn("Malformed selected attempt" if malformed else "ownership conflict", output)
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task a\s+Reuse\s+")
        self.assertEqual((self.work / "results/samples/a/report/renamed.txt").read_text(), "first")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])
