"""Installed factory packages and the executable managed-storage example."""

import os
from pathlib import Path
import shutil
import json
import unittest

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
        self.cli("clean", "--delete")
        self.assertNoSubmissions(self.cli("run"))


class PackagedExampleTests(LocalBackendTestCase):
    slurm_environment = test_managed_recovery.ManagedCoordinationTests.slurm_environment
    def configure_workflow(self):
        shutil.copy(EXAMPLE / "workflow.py", self.work)
        shutil.copytree(EXAMPLE / "data", self.work / "data")

    def test_installed_producers_clean_then_new_consumer_computes_only_once(self):
        producers_only = {**os.environ, "GWFLOW_EXAMPLE_REPORT": "0"}
        preview = self.cli("run", "A", "B", "--dry-run", env=producers_only)
        self.assertRegex(preview, r"Task A\s+Run\s+")
        self.assertRegex(preview, r"Task B\s+Run\s+")
        self.assertNotIn("Task C", preview)
        first = self.cli("run", "A", "B", "--details", env=producers_only)
        self.finish()
        self.assertIn("A__clean: confirmed submitted", first)
        self.assertIn("B__clean: confirmed submitted", first)
        self.assertNotRegex(first, r"C__\w+: confirmed submitted")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for root in (self.work / ".gwf/gwflow", self.work / "work", self.work / "results")
                  for path in root.rglob("*") if path.is_file()}
        preview = self.cli("clean", "A", "B", env=producers_only)
        self.assertIn("eligible", preview)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)
        self.cli("clean", "A", "B", "--delete", env=producers_only)
        preview = self.cli("run", "C", "--dry-run")
        self.assertRegex(preview, r"Task A\s+Reuse\s+")
        self.assertRegex(preview, r"Task B\s+Reuse\s+")
        self.assertRegex(preview, r"Task C\s+Run\s+")
        second = self.cli("run", "C", "--details")
        self.finish()
        self.assertIn("C__join: confirmed submitted", second)
        self.assertNotRegex(second, r"[AB]__\w+: confirmed submitted")
        self.assertEqual((self.work / "results/net/net.csv").read_text().splitlines(),
                         ["product,sales,returns,net", "apples,17,2,15", "pears,8,1,7"])
        self.assertEqual(sorted(str(path.relative_to(self.work / "results"))
                                for path in (self.work / "results").rglob("*") if path.is_file()),
                         ["net/net.csv", "returns/summary.csv", "sales/summary.csv"])
        for name in ("A", "B"):
            self.assertFalse(list((self.work / "work" / name).iterdir()))
        self.assertNoSubmissions(self.cli("run"))

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


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SUMMARY_SIF") and os.environ.get("GWFLOW_TEST_REPORT_SIF"),
                     "set prepared summary/report SIFs for real packaged container execution")
class PackagedContainerExampleTests(LocalBackendTestCase):
    def configure_workflow(self):
        shutil.copy(EXAMPLE / "container-workflow.py", self.work / "workflow.py")
        shutil.copytree(EXAMPLE / "data", self.work / "data")
        (self.work / "images").mkdir()
        for name in ("summary", "report"):
            (self.work / "images" / f"{name}.sif").symlink_to(os.environ[f"GWFLOW_TEST_{name.upper()}_SIF"])

    def test_packaged_containers_clean_producers_then_compute_only_the_new_consumer(self):
        PackagedExampleTests.test_installed_producers_clean_then_new_consumer_computes_only_once(self)
        for target in ("A__clean", "B__aggregate", "C__join"):
            self.assertIn(" image ", self.cli("logs", target, "--stderr", "--no-pager"))
        details = self.cli("run", "--dry-run", "--details")
        consumer = details.split("Details for Task C:\n", 1)[1].split("Details for Task ", 1)[0]
        workspace = Path(next(line.split("Workspace: ", 1)[1]
                              for line in consumer.splitlines() if "Workspace: " in line))
        for name in ("sales", "returns"):
            staged = list(workspace.rglob(f"{name}/summary.csv"))
            self.assertEqual(len(staged), 1)
            self.assertTrue(staged[0].is_symlink())
            self.assertTrue(staged[0].samefile(self.work / "results" / name / "summary.csv"))
