"""Installed factory packages and the executable managed-storage example."""

import os
from pathlib import Path
import shutil
import json

import test_managed_recovery

from support import LocalBackendTestCase


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "packaged"


class UppercaseExampleTests(LocalBackendTestCase):
    def configure_workflow(self):
        for name in ("workflow.py", "task_library.py", "input.txt"):
            shutil.copy(EXAMPLE.parent / "uppercase" / name, self.work)

    def test_independent_factory_instances_retain_uppercase_text(self):
        self.run_complete()
        for name in ("alpha", "beta"):
            self.assertEqual((self.work / "results" / name / "text.txt").read_text(),
                             "HELLO FROM GWFLOW\n")
        self.cli("clean-work", "--delete")
        self.assertNotIn("Submitted target", self.cli("run"))


class PackagedExampleTests(LocalBackendTestCase):
    slurm_environment = test_managed_recovery.ManagedCoordinationTests.slurm_environment
    def configure_workflow(self):
        shutil.copy(EXAMPLE / "workflow.py", self.work)
        shutil.copytree(EXAMPLE / "data", self.work / "data")

    def test_installed_producers_clean_then_new_consumer_computes_only_once(self):
        producers_only = {**os.environ, "GWFLOW_EXAMPLE_REPORT": "0"}
        first = self.cli("run", env=producers_only)
        self.finish()
        self.assertIn("Submitted target A__clean", first)
        self.assertIn("Submitted target B__clean", first)
        self.assertNotIn("Submitted target C__", first)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for root in (self.work / ".gwf/gwflow", self.work / "work", self.work / "results")
                  for path in root.rglob("*") if path.is_file()}
        preview = self.cli("clean-work", env=producers_only)
        self.assertIn("eligible", preview)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)
        self.cli("clean-work", "--delete", env=producers_only)
        second = self.run_complete()
        self.assertIn("Submitted target C__join", second)
        self.assertNotIn("Submitted target A__", second)
        self.assertNotIn("Submitted target B__", second)
        self.assertEqual((self.work / "results/net/net.csv").read_text().splitlines(),
                         ["product,sales,returns,net", "apples,17,2,15", "pears,8,1,7"])
        self.assertEqual(sorted(str(path.relative_to(self.work / "results"))
                                for path in (self.work / "results").rglob("*") if path.is_file()),
                         ["net/net.csv", "returns/summary.csv", "sales/summary.csv"])
        for name in ("A", "B"):
            self.assertFalse(list((self.work / "work" / name).iterdir()))
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_packaged_slurm_dependencies_wait_for_both_complete_producers(self):
        self.cli("-b", "slurm", "run", env=self.slurm_environment())
        self.finish()
        submissions = [json.loads(line) for line in (self.work / "slurm-submitted.jsonl").read_text().splitlines()]
        completed = [item["id"] for item in submissions if item["name"].startswith(("A__gwflow_complete__", "B__gwflow_complete__"))]
        prepared = next(item for item in submissions if item["name"].startswith("C__gwflow_prepare__"))
        self.assertCountEqual(prepared["args"][1].removeprefix("--dependency=afterok:").split(":"), completed)
        for item in submissions:
            if item["name"].startswith(("A__gwflow_prepare__", "B__gwflow_prepare__")):
                self.assertEqual(item["args"], ["--parsable"])
        self.assertEqual((self.work / "results/net/net.csv").read_text().splitlines(),
                         ["product,sales,returns,net", "apples,17,2,15", "pears,8,1,7"])
