"""The public representation-switching example through the installed CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from support import LocalBackendTestCase


EXAMPLE = Path(__file__).resolve().parents[1] / "example/logical_inputs"


class LogicalInputExampleTests(LocalBackendTestCase):
    def configure_workflow(self):
        for name in ("workflow.py", "materialize.py", "representation.txt"):
            shutil.copyfile(EXAMPLE / name, self.work / name)
        shutil.copytree(EXAMPLE / "parts", self.work / "parts")
        if image := os.environ.get("GWFLOW_TEST_SIF"):
            (self.work / "python.sif").symlink_to(Path(image).resolve())
        else:
            # Planning can inspect a regular file. Only the fixture-gated test runs it.
            (self.work / "python.sif").touch()

    def switch_to_archive(self):
        subprocess.run([sys.executable, "-m", "zipfile", "-c", "document.zip", "parts"],
                       cwd=self.work, check=True)
        shutil.rmtree(self.work / "parts")
        (self.work / "representation.txt").write_text("zip\n")

    def test_example_plans_both_physical_representations(self):
        for selection in ("files", "zip"):
            with self.subTest(selection=selection):
                if selection == "zip":
                    self.switch_to_archive()
                self.assertIn("Would submit 4 jobs", self.cli("run", "--dry-run"))

    @unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
    def test_clean_switch_reuse_and_forced_archive_computation(self):
        self.run_complete()
        result = self.work / "results/document/report.txt"
        expected = "Meeting notes\nThe meeting starts at noon.\n"
        self.assertEqual(result.read_text(), expected)
        attempt = self.task_attempts()["document"]
        logs = ("logs", "document__materialize", "--no-pager")
        self.assertRegex(self.cli(*logs), r"Materialized files from .*/inputs/source\.json")
        intermediates = list((self.work / "work/document").rglob("committed/heading.txt"))
        self.assertEqual(len(intermediates), 1)
        descriptor = intermediates[0].parent / "inputs/source.json"
        members = json.loads(descriptor.read_text())["files"]
        self.assertEqual(set(members), {"heading", "body"})
        for key, member in members.items():
            self.assertTrue(Path(member).is_symlink())
            self.assertEqual(Path(member).resolve(), self.work / "parts" / key / "text.txt")
        self.cli("clean", "document", "--delete")
        self.assertFalse(intermediates[0].exists())
        self.switch_to_archive()
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual((status["attempt_id"], status["next_action"]), (attempt, "reuse"))
        self.assertIn("Reuse", self.cli("run", "--dry-run"))
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual(self.task_attempts()["document"], attempt)
        self.assertFalse(intermediates[0].exists())
        self.assertEqual(result.read_text(), expected)

        self.run_complete("document", "--force")
        self.assertNotEqual(self.task_attempts()["document"], attempt)
        self.assertIn("Materialized zip", self.cli(*logs))
        self.assertEqual(result.read_text(), expected)
