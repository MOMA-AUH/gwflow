"""Run the installed task packages as a three-task local workflow."""

from importlib.metadata import version
from pathlib import Path
import shutil

from test_reuse import LocalBackendTestCase


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "packaged"


class PackagedExampleTests(LocalBackendTestCase):
    def configure_workflow(self):
        shutil.copy(EXAMPLE / "workflow.py", self.work)
        shutil.copytree(EXAMPLE / "data", self.work / "data", dirs_exist_ok=True)

    def test_installed_packages_compose_and_reuse_after_cleanup(self):
        self.assertEqual(version("gwflow-summary-task"), "0.1.0")
        self.assertEqual(version("gwflow-report-task"), "0.1.0")

        first = self.run_complete()
        for name in ("A", "B", "C"):
            self.assertIn(f"Submitted target {name}__gwflow_complete", first)
        self.assertEqual(
            (self.work / "results" / "net.csv").read_text(),
            "product,sales,returns,net\napples,17,2,15\npears,8,1,7\n",
        )

        intermediates = [
            self.work / "work" / name
            for name in ("A.cleaned.csv", "B.cleaned.csv", "C.joined.csv")
        ]
        for path in intermediates:
            self.assertTrue(path.exists())
            path.unlink()
        second = self.cli("run")
        self.assertNotIn("Submitted target", second)
        self.assertFalse(any(path.exists() for path in intermediates))
