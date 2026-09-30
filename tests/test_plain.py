"""Installing gwflow must preserve ordinary gwf command behavior."""

from support import LocalBackendTestCase


class PlainWorkflowTests(LocalBackendTestCase):
    def configure_workflow(self, **options):
        (self.work / "workflow.py").write_text(
            "from pathlib import Path\n"
            "from gwf import Workflow\n"
            "with Path('loads.txt').open('a') as stream: stream.write('loaded\\n')\n"
            "gwf = Workflow()\n"
            "gwf.target('selected', inputs=[], outputs=['selected.txt']) << 'echo log-message; touch selected.txt'\n"
            "gwf.target('other', inputs=[], outputs=['other.txt']) << 'touch other.txt'\n"
        )

    def test_run_loads_once_preserves_selection_and_logs(self):
        output = self.cli("run", "selected")
        self.finish()
        self.assertEqual((self.work / "loads.txt").read_text(), "loaded\n")
        self.assertIn("Submitted target selected", output)
        self.assertNotIn("Submitted target other", output)
        self.assertTrue((self.work / "selected.txt").exists())
        self.assertFalse((self.work / "other.txt").exists())
        self.assertIn("log-message", self.cli("logs", "selected", "--no-pager"))

    def test_status_formats_load_once(self):
        for output_format in ("tree", "default", "summary", "grouped"):
            with self.subTest(output_format=output_format):
                (self.work / "loads.txt").unlink(missing_ok=True)
                self.cli("status", "--format", output_format)
                self.assertEqual((self.work / "loads.txt").read_text(), "loaded\n")

    def test_generic_clean_and_touch_still_work(self):
        self.cli("touch", "selected")
        self.assertTrue((self.work / "selected.txt").exists())
        self.assertNotIn("Submitted target", self.cli("run", "selected"))
        self.cli("clean", "--all", "--force", "selected")
        self.assertFalse((self.work / "selected.txt").exists())

    def test_dry_run_and_force_are_unchanged(self):
        self.assertIn("Would submit selected", self.cli("run", "selected", "--dry-run"))
        self.assertFalse((self.work / "selected.txt").exists())
        self.cli("run", "selected")
        self.finish()
        self.assertIn("Submitted target selected", self.cli("run", "selected", "--force"))
