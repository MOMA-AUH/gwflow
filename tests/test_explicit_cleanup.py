"""Explicit inactive-attempt cleanup through the public CLI."""

from collections import Counter
import re
import json
import shlex
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_cleanup
import test_graphs
import test_transfer


class ExplicitCleanupTests(LocalBackendTestCase):
    configure_workflow = test_cleanup.CompletedWorkCleanupTests.configure_workflow
    settle = test_cleanup.CompletedWorkCleanupTests.settle
    inject = test_cleanup.CompletedWorkCleanupTests.inject

    def attempt(self, name):
        return next(line.split("Attempt: ")[1] for line in self.cli("explain", "--details", name).splitlines()
                    if "Attempt:" in line)

    def test_selected_older_attempt_preserves_current_results_and_reuse(self):
        self.run_complete()
        older = self.attempt("a")
        self.cli("run", "--force-task", "a")
        self.finish()
        current = self.attempt("a")
        roots = [self.work / ".gwf/gwflow", self.work / "work", self.work / "results"]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for root in roots for path in root.rglob("*") if path.is_file()}
        preview = self.cli("clean-work", "--attempt", older)
        self.assertIn(f"Task a attempt {older}: eligible", preview)
        self.assertNotIn(current, preview)
        self.assertIn("progress", preview)
        self.assertIn("diagnostics", preview)
        self.assertIn("repair sources", preview)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                          for root in roots for path in root.rglob("*") if path.is_file()}, before)
        self.cli("clean-work", "--delete", "--attempt", older)
        self.assertFalse((self.work / "work/a" / older).exists())
        self.assertTrue((self.work / "work/a" / current).exists())
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1})

    def test_failed_attempt_cleanup_discards_successful_progress_but_preserves_logs(self):
        failing = self.work / "fail-right"
        failing.touch()
        test_graphs.TaskGraphTests.configure_workflow(self, right_command=(
            f"echo right >> {shlex.quote(str(self.work / 'trace'))}; echo failure-diagnostic; "
            f"if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt"
        ))
        self.cli("run")
        self.settle()
        previous = self.attempt("sample")
        self.assertRegex(self.cli("explain"), r"Task sample\s+Retry\s+")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 1, "right": 1})
        logs = self.cli("logs", "sample__right", "--no-pager")
        self.assertIn("failure-diagnostic", logs)
        self.cli("clean-work", "--delete", "--attempt", previous)
        self.assertFalse((self.work / "work/sample" / previous).exists())
        self.assertEqual(self.cli("logs", "sample__right", "--no-pager"), logs)
        self.assertEqual((self.work / "input.txt").read_text(), "hello\n")
        self.assertRegex(self.cli("explain"), r"Task sample\s+Run\s+")
        failing.unlink()
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt("sample"), previous)
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 2, "right": 2, "join": 1})

    def test_multiple_exact_selections_and_invalid_selectors_before_any_deletion(self):
        self.run_complete()
        a, b = self.attempt("a"), self.attempt("b")
        for flags in (("--attempt", a, "--attempt", "not-recorded"),
                      ("--attempt", a, "--task", "b"), ("--attempt", "f" * 32)):
            with self.subTest(flags=flags):
                self.cli("clean-work", "--delete", *flags, success=False)
                self.assertTrue((self.work / "work/a" / a).exists())
                self.assertTrue((self.work / "work/b" / b).exists())
        self.cli("clean-work", "--delete", "--attempt", a, "--attempt", b, "--attempt", a)
        for name, attempt in (("a", a), ("b", b)):
            self.assertFalse((self.work / "work" / name / attempt).exists())
            self.assertEqual((self.work / "results" / name / "result.txt").read_text(), name)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_active_attempt_refuses_explicit_deletion_without_cleaning_other_selections(self):
        held, release = self.work / "held", self.work / "release"
        wait = f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("echo a", wait + "echo a"))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            self.wait_for(lambda: re.search(r"Task b\s+Reuse\s+", self.cli("explain", "b")))
            a, b = self.attempt("a"), self.attempt("b")
            preview = self.cli("clean-work", "--attempt", a)
            self.assertIn(f"Task a attempt {a}: blocked; active work", preview)
            output = self.cli("clean-work", "--delete", "--attempt", a, "--attempt", b, success=False)
            self.assertIn("active work", output)
            self.assertTrue((self.work / "work/a" / a).exists())
            self.assertTrue((self.work / "work/b" / b).exists())
        finally:
            release.touch()
        self.finish()
        self.cli("clean-work", "--delete", "--attempt", a)
        self.assertFalse((self.work / "work/a" / a).exists())

    def test_uncertain_attempt_refuses_explicit_deletion(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="a__compute"), success=False)
        self.settle()
        attempt = self.attempt("a")
        output = self.cli("clean-work", "--delete", "--attempt", attempt, success=False)
        self.assertIn("unresolved submission", output)
        self.assertTrue((self.work / "work/a" / attempt).exists())

    def test_cleanup_before_input_preparation_requires_fresh_attempt(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        a, b = self.attempt("a"), self.attempt("b")
        self.cli("clean-work", "--delete", "--attempt", a, "--attempt", b)
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.assertRegex(self.cli("explain"), r"Task b\s+Run\s+")
        self.run_complete()
        self.assertNotEqual(self.attempt("a"), a)
        self.assertNotEqual(self.attempt("b"), b)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 1})

    def test_cleaned_failed_attempt_uses_new_inputs_instead_of_discarded_baseline(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("empty_task(inputs=[])", "empty_task(inputs=['input.txt'])", 1)
                            .replace("printf a", "exit 8; printf a"))
        self.cli("run")
        self.settle()
        attempt = self.attempt("a")
        baseline = next(path for path in (self.work / ".gwf/gwflow").rglob("inputs.json")
                        if json.loads(path.read_text())["attempt"] == attempt)
        baseline.write_text("{")
        self.cli("clean-work", "--delete", "--attempt", attempt)
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        (self.work / "input.txt").unlink()
        self.assertIn("Cannot observe external input", self.cli("run", success=False))
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 1})
        (self.work / "input.txt").write_text("new baseline\n")
        workflow.write_text(workflow.read_text().replace("exit 8; printf a", "printf a"))
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt("a"), attempt)
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")

    def test_interrupted_failed_cleanup_resumes_and_never_continues_discarded_work(self):
        failing = self.work / "fail-b"
        failing.touch()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "printf b", f"if [ -e {shlex.quote(str(failing))} ]; then touch diagnostic; exit 8; fi; printf b"))
        for index, phase in enumerate(("cleanup_before_remove", "cleanup_during_remove",
                                       "cleanup_after_workspace_remove", "cleanup_before_completion")):
            with self.subTest(phase=phase):
                self.cli("run", *(("--force-task", "b") if index else ()))
                self.settle()
                attempt = self.attempt("b")
                outcome = subprocess.run([
                    sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "b",
                    "clean-work", "--delete", "--attempt", attempt,
                ], cwd=self.work, capture_output=True, text=True, timeout=30)
                self.assertIn(outcome.returncode, (102, 103, 104, 105), outcome.stdout + outcome.stderr)
                self.assertRegex(self.cli("explain"), r"Task b\s+Run\s+")
                self.cli("clean-work", "--delete", "--attempt", attempt)
                self.assertFalse((self.work / "work/b" / attempt).exists())
                self.assertIn("already removed", self.cli("clean-work", "--delete", "--attempt", attempt))
                self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        failing.unlink()
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt("b"), attempt)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 5})
        self.assertEqual((self.work / "results/b/result.txt").read_text(), "b")

    def test_explicit_repair_source_cleanup_requires_fresh_computation_after_damage(self):
        self.run_complete()
        attempt = self.attempt("a")
        result = self.work / "results/a/result.txt"
        result.write_text("manually edited")
        self.assertRegex(self.cli("explain"), r"Task a\s+Repair\s+")
        preview = self.cli("clean-work", "--attempt", attempt)
        self.assertIn("repair sources", preview)
        self.cli("clean-work", "--delete", "--attempt", attempt)
        self.assertEqual(result.read_text(), "manually edited")
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.run_complete()
        self.assertNotEqual(self.attempt("a"), attempt)
        self.assertEqual(result.read_text(), "a")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1})

    def test_missing_workspace_ownership_refuses_all_selected_deletion(self):
        self.run_complete()
        a, b = self.attempt("a"), self.attempt("b")
        ready = next(path for path in (self.work / ".gwf/gwflow").rglob("ready.json")
                     if json.loads(path.read_text())["attempt"] == b)
        record = json.loads(ready.read_text())
        del record["workspace_identity"]
        ready.write_text(json.dumps(record))
        output = self.cli("clean-work", "--delete", "--attempt", a, "--attempt", b, success=False)
        self.assertIn("workspace ownership", output)
        for name, attempt in (("a", a), ("b", b)):
            self.assertTrue((self.work / "work" / name / attempt).exists())
            self.assertEqual((self.work / "results" / name / "result.txt").read_text(), name)


class ExplicitStagingCleanupTests(LocalBackendTestCase):
    configure_workflow = test_transfer.TransferRecoveryTests.configure_workflow
    assert_results = test_transfer.TransferRecoveryTests.assert_results
    settle = test_transfer.TransferRecoveryTests.settle
    inject = test_transfer.TransferRecoveryTests.inject
    fault = test_transfer.TransferRecoveryTests.fault
    separate_filesystem = test_transfer.TransferRecoveryTests.separate_filesystem
    attempt = ExplicitCleanupTests.attempt

    def test_explicit_failed_transfer_cleanup_removes_configured_staging_and_recomputes(self):
        remote = self.separate_filesystem()
        self.configure_workflow(settings=f"results_root={str(remote / 'results')!r}, results_staging_root={str(remote / 'staging')!r}")
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        attempt = self.attempt("a")
        self.assertRegex(self.cli("explain"), r"Task a\s+Finish\s+")
        self.assertIn("keep", self.cli("clean-work"))
        preview = self.cli("clean-work", "--attempt", attempt)
        self.assertIn("eligible", preview)
        self.assertIn("Staging: " + str(remote / "staging"), preview)
        leftovers = [path for path in (remote / "staging").rglob("*") if path.is_file()]
        self.assertTrue(leftovers)
        self.cli("clean-work", "--delete", "--attempt", attempt)
        self.assertTrue(all(not path.exists() for path in leftovers))
        self.assertFalse((self.work / "work/a" / attempt).exists())
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt("a"), attempt)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])
        self.assert_results(remote / "results/samples/a/report")
