"""Whole-workflow preview, force, and selector behavior through gwf's CLI."""

import json
import os
import shutil

import test_reuse


class ExecutionModeCliTests(test_reuse.LocalBackendTestCase):
    def records(self):
        directory = self.work / ".gwf" / "gwflow"
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*.json")} if directory.exists() else {}

    def expected(self):
        return json.loads((self.work / ".gwf" / "gwflow" / "text" / "expected.json").read_text())

    def preview_without_submissions(self, *args):
        # This backend rejects submit(), so the preview cannot accidentally
        # launch fixture work even if gwf's dry-run plumbing changes.
        shutil.copy(test_reuse.FIXTURES / "state_backend.py", self.work)
        metadata = self.work / "state_backend-1.0.dist-info"
        metadata.mkdir(exist_ok=True)
        (metadata / "entry_points.txt").write_text(
            "[gwf.backends]\nstate_fixture = state_backend:setup\n"
        )
        (self.work / "backend-state.json").write_text("{}")
        return self.cli("-b", "state_fixture", "run", "--dry-run", *args,
                        env={**os.environ, "PYTHONPATH": str(self.work)})

    def check_preview_and_force(self, tracking):
        self.configure(use_spec_hashes=tracking)
        initial = self.preview_without_submissions()
        for name in ("prepare", "finish", "gwflow_complete"):
            self.assertIn(f"Would submit text__{name}", initial)
        self.assertEqual(self.records(), {})
        self.assertFalse((self.work / "trace.txt").exists())

        self.run_complete()
        original = self.expected()
        before = self.records()
        self.assertNotIn("Would submit", self.preview_without_submissions())
        self.assertEqual(self.records(), before)

        # A retained output can be fresh while an intermediate is gone.
        (self.work / "middle.txt").unlink()
        self.assertNotIn("Would submit", self.preview_without_submissions())
        forced = self.preview_without_submissions("--force")
        for name in ("prepare", "finish", "gwflow_complete"):
            self.assertIn(f"Would submit text__{name}", forced)
        self.assertEqual(self.records(), before)
        self.assertFalse((self.work / "middle.txt").exists())

        output = self.run_forced()
        for name in ("prepare", "finish", "gwflow_complete"):
            self.assertIn(f"Submitted target text__{name}", output)
        self.assertNotEqual(self.expected()["attempt"], original["attempt"])
        self.assertEqual(self.trace(), {"prepare": 2, "finish": 2})

    def test_preview_and_force_with_tracking_disabled(self):
        self.check_preview_and_force(False)

    def test_preview_and_force_with_tracking_enabled(self):
        self.check_preview_and_force(True)

    def run_forced(self):
        output = self.cli("run", "--force")
        self.finish()
        return output

    def check_nonreusable_bookkeeping(self, tracking):
        self.configure(use_spec_hashes=tracking)
        self.run_complete()
        directory = self.work / ".gwf" / "gwflow" / "text"
        original = self.records()
        cases = (
            ("missing expected", directory / "expected.json", None),
            ("invalid expected", directory / "expected.json", b"not JSON"),
            ("missing record", directory / (self.expected()["attempt"] + ".json"), None),
            ("invalid record", directory / (self.expected()["attempt"] + ".json"), b"not JSON"),
        )
        for label, path, content in cases:
            with self.subTest(case=label):
                path.write_bytes(content) if content is not None else path.unlink()
                damaged = self.records()
                preview = self.preview_without_submissions()
                self.assertIn("Would submit text__gwflow_complete", preview)
                self.assertEqual(self.records(), damaged)
                forced = self.preview_without_submissions("--force")
                for name in ("prepare", "finish", "gwflow_complete"):
                    self.assertIn(f"Would submit text__{name}", forced)
                self.assertEqual(self.records(), damaged)
                self.assertEqual(self.trace(), {"prepare": 1, "finish": 1})
                for name, value in original.items():
                    (self.work / ".gwf" / "gwflow" / name).write_bytes(value)

    def test_nonreusable_bookkeeping_with_tracking_disabled(self):
        self.check_nonreusable_bookkeeping(False)

    def test_nonreusable_bookkeeping_with_tracking_enabled(self):
        self.check_nonreusable_bookkeeping(True)

    def test_force_resubmits_active_targets_and_replaces_expectation(self):
        self.configure_workflow(side_gate=True)
        self.cli("run")
        self.wait_for(lambda: (self.work / "side_started").exists())
        original = self.expected()
        preview = self.cli("run", "--force", "--dry-run")
        for name in ("prepare", "finish", "side", "gwflow_complete"):
            self.assertIn(f"Would submit text__{name}", preview)
        self.assertEqual(self.expected(), original)
        output = self.cli("run", "--force")
        for name in ("prepare", "finish", "side", "gwflow_complete"):
            self.assertIn(f"Submitted target text__{name}", output)
        self.assertNotEqual(self.expected()["attempt"], original["attempt"])
        (self.work / "release").touch()
        self.finish()

    def test_unsupported_selectors_fail_before_submission(self):
        for args in (("text__prepare",), ("--group", "text"), ("--no-deps",),
                     ("--force", "--dry-run", "text__finish")):
            with self.subTest(args=args):
                output = self.cli("run", *args, success=False)
                self.assertIn("whole-workflow run only", output)
                self.assertEqual(self.records(), {})
                self.assertFalse((self.work / "trace.txt").exists())

    def test_selector_is_rejected_without_task_declarations(self):
        (self.work / "workflow.py").write_text(
            "from gwflow import Workflow\n"
            "gwf = Workflow()\n"
            "gwf.target('plain', inputs=['input.txt'], outputs=['plain.txt']) << "
            "'cp input.txt plain.txt'\n"
        )
        output = self.cli("run", "plain", success=False)
        self.assertIn("whole-workflow run only", output)
        self.assertFalse((self.work / "plain.txt").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
