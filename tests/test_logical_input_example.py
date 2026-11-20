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

    def assert_materialization(self, workspace, sources):
        materialized, = workspace.glob("materialize/*/committed")
        joined, = workspace.glob("join/*/committed")
        for name in ("heading.txt", "body.txt"):
            intermediate = materialized / name
            self.assertTrue(intermediate.is_file())
            self.assertFalse(intermediate.is_symlink())
            self.assertTrue((joined / name).is_symlink())
            self.assertEqual((joined / name).resolve(), intermediate)

        descriptor = json.loads((materialized / "inputs/source.json").read_text())
        members = descriptor["files"]
        self.assertEqual(set(members), set(sources))
        for key, source in sources.items():
            self.assertTrue(Path(members[key]).is_symlink())
            self.assertEqual(Path(members[key]).resolve(), source)

    @unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
    def test_clean_switch_reuse_and_forced_archive_computation(self):
        self.run_complete()
        result = self.work / "results/document/report.txt"
        expected = "Meeting notes\nThe meeting starts at noon.\n"
        self.assertEqual(result.read_text(), expected)
        retained = result.stat()
        attempt = self.task_attempts()["document"]
        workspace = self.work / "work/document" / attempt
        logs = ("logs", "document__materialize", "--no-pager")
        self.assertRegex(self.cli(*logs), r"Materialized files from .*/inputs/source\.json")
        sources = {key: self.work / "parts" / key / "text.txt" for key in ("heading", "body")}
        self.assert_materialization(workspace, sources)
        self.cli("clean", "document", "--delete")
        self.assertFalse(workspace.exists())
        self.switch_to_archive()
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual((status["attempt_id"], status["next_action"]), (attempt, "reuse"))
        self.assertIn("Reuse", self.cli("run", "--dry-run"))
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual(self.task_attempts()["document"], attempt)
        self.assertFalse(workspace.exists())
        reused = result.stat()
        self.assertEqual((reused.st_ino, reused.st_mtime_ns), (retained.st_ino, retained.st_mtime_ns))
        self.assertEqual(result.read_text(), expected)

        self.run_complete("document", "--force")
        fresh = self.task_attempts()["document"]
        self.assertNotEqual(fresh, attempt)
        self.assert_materialization(self.work / "work/document" / fresh, {"archive": self.work / "document.zip"})
        self.assertIn("Materialized zip", self.cli(*logs))
        self.assertEqual(result.read_text(), expected)
