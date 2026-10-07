"""Scoped managed reads through installed CLI and real filesystem faults."""

import shutil

from support import LocalBackendTestCase
import test_planning_io


class DirectoryObservationTests(LocalBackendTestCase):
    configure_workflow = test_planning_io.ProducerEvidenceTests.configure_workflow
    probe = test_planning_io.PlanningObservationTests.probe
    scenario = test_planning_io.PlanningObservationTests.scenario

    def test_prefix_opens_do_not_multiply_with_records_or_path_depth(self):
        root = self.work
        for consumers, references, depth in ((1, 1, 0), (1, 3, 0), (4, 1, 0), (1, 1, 12), (1, 3, 12)):
            with self.subTest(consumers=consumers, references=references, depth=depth):
                self.work = root / f"case-{consumers}-{references}-{depth}"
                for _ in range(depth):
                    self.work /= "level"
                self.work.mkdir(parents=True)
                (self.work / "input.txt").write_text("reference\n")
                self.configure()
                self.configure_workflow(consumers, references)
                self.evidence_label += f"depth-{depth}-"
                self.run_complete()
                output, report = self.probe("status")
                self.assertEqual(output, self.cli_result("status").stdout)
                self.assertLessEqual(report["paths"]["planning"].get("directory_opens:/", 0), 8)
                self.assertLessEqual(report["counts"]["planning"]["directory_opens"],
                                     100 + 6 * depth + 30 * consumers)
                self.assertLessEqual(report["peak_opened_descriptors"], 66)

    def test_opened_bookkeeping_root_replacement_is_rejected_then_accepted_next_pass(self):
        self.run_complete()
        root = self.work / ".gwf/gwflow"
        replacement = self.work / "replacement"
        saved = self.work / "displaced"
        shutil.copytree(root, replacement)
        before, after = self.scenario(
            [["status", "--details"], ["status", "--details"]],
            [{"pass": 0, "after": str(root / "owner.json"),
              "rename": {str(root): str(saved), str(replacement): str(root)}}],
        )
        self.assertNotEqual(before["exit_code"], 0, before["output"])
        self.assertIn("Managed directory changed", before["output"])
        self.assertEqual(after["exit_code"], 0, after["output"])
        self.assertIn("2 reusable", after["output"])
        self.assertEqual((self.work / "results/sample0/copy.txt").read_text(), "hello\n")
        for result in (before, after):
            self.assertEqual(result["directories_before"], result["directories_after"])

    def test_symlink_substitution_of_opened_directory_never_reads_outside_evidence(self):
        self.run_complete()
        root = self.work / ".gwf/gwflow"
        outside = self.work / "outside"
        shutil.copytree(root, outside)
        sentinel = outside / "sentinel"
        sentinel.write_text("outside data")
        snapshot = {str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
        result, = self.scenario(
            [["status", "--details"]],
            [{"pass": 0, "after": str(root / "owner.json"),
              "rename": {str(root): str(self.work / "displaced")}, "symlink": {str(root): str(outside)}}],
        )
        self.assertNotEqual(result["exit_code"], 0, result["output"])
        self.assertIn("Managed directory changed", result["output"])
        self.assertEqual(snapshot, {str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()})
        self.assertEqual(result["directories_before"], result["directories_after"])

    def test_resources_close_after_repeated_success_failure_and_interruption(self):
        self.run_complete()
        owner = str(self.work / ".gwf/gwflow/owner.json")
        for failure in (None, "error", "interrupt"):
            with self.subTest(failure=failure):
                actions = [] if failure is None else [{"pass": 0, "after": owner, failure: True}]
                results = self.scenario([["status"], ["explain"], ["run", "--dry-run"], ["run"]], actions)
                for index, result in enumerate(results):
                    self.assertEqual(result["directories_before"], result["directories_after"])
                    if index or failure is None:
                        self.assertEqual(result["exit_code"], 0, result["output"])
                        self.assertRegex(result["output"], "reusable|Reuse")
                    else:
                        self.assertNotEqual(result["exit_code"], 0)

    def test_very_deep_paths_use_bounded_descriptors(self):
        self.work = self.work.joinpath(*(["deep"] * 72))
        self.work.mkdir(parents=True)
        (self.work / "input.txt").write_text("hello\n")
        self.configure()
        self.configure_workflow()
        self.run_complete()
        for command in (("status",), ("explain",), ("run", "--dry-run"), ("run",)):
            with self.subTest(command=command):
                output, report = self.probe(*command)
                self.assertEqual(output, self.cli_result(*command).stdout)
                self.assertLessEqual(report["peak_opened_descriptors"], 67)
