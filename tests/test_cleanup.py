"""Owned work cleanup through the installed CLI and real local workers."""

from collections import Counter
import shlex
import subprocess
import sys

from support import FIXTURES, GWF, LocalBackendTestCase
import test_fresh
import test_transfer


class CompletedWorkCleanupTests(LocalBackendTestCase):
    settle = test_fresh.FreshAttemptTests.settle
    inject = test_fresh.FreshAttemptTests.inject

    def configure_workflow(self):
        test_fresh.FreshAttemptTests.configure_workflow(self)
        workflow = self.work / "workflow.py"
        self.with_consumer = workflow.read_text()
        workflow.write_text(self.with_consumer.split("task = Task(inputs=[a.outputs")[0])

    def test_preview_then_delete_preserves_results_and_only_new_consumer_computes(self):
        self.run_complete()
        roots = [self.work / ".gwf/gwflow", self.work / "work", self.work / "results"]
        before = {str(path): path.read_bytes() for root in roots for path in root.rglob('*') if path.is_file()}
        preview = self.cli("clean-work")
        for name in ("a", "b"):
            self.assertIn(f"Task {name}", preview)
            self.assertIn(str(self.work / "work" / name), preview)
        self.assertIn("eligible", preview)
        self.assertIn("--delete", preview)
        self.assertEqual({str(path): path.read_bytes() for root in roots for path in root.rglob('*') if path.is_file()}, before)
        self.cli("clean-work", "--delete")
        for name in ("a", "b"):
            self.assertEqual(list((self.work / "work" / name).iterdir()), [])
            self.assertEqual((self.work / "results" / name / "result.txt").read_text(), name)
        (self.work / "workflow.py").write_text(self.with_consumer)
        submitted = self.run_complete()
        self.assertIn("Submitted target c__compute", submitted)
        self.assertNotIn("Submitted target a__", submitted)
        self.assertNotIn("Submitted target b__", submitted)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 1, "c": 1})
        self.assertNotIn("Submitted target", self.cli("run"))
        for name in ("a", "b"):
            self.assertEqual(list((self.work / "work" / name).iterdir()), [])

    def test_exact_task_selection_and_unknown_selection_before_any_deletion(self):
        self.run_complete()
        self.assertIn("Unknown Task", self.cli("clean-work", "--delete", "--task", "a", "--task", "missing", success=False))
        self.assertTrue(list((self.work / "work/a").iterdir()))
        self.assertTrue(list((self.work / "work/b").iterdir()))
        self.cli("clean-work", "--delete", "--task", "a", "--task", "a")
        self.assertFalse(list((self.work / "work/a").iterdir()))
        self.assertTrue(list((self.work / "work/b").iterdir()))

    def test_superseded_attempt_is_reported_and_kept_by_default(self):
        self.run_complete()
        previous = next((self.work / "work/a").iterdir())
        self.cli("run", "--force-task", "a")
        self.finish()
        preview = self.cli("clean-work")
        self.assertIn(previous.name, preview)
        self.assertIn("superseded", preview)
        self.cli("clean-work", "--delete")
        self.assertEqual(list((self.work / "work/a").iterdir()), [previous])
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")

    def test_damaged_results_keep_work_and_cleaned_sources_require_fresh_attempt(self):
        self.run_complete()
        previous = next((self.work / "work/a").iterdir())
        result = self.work / "results/a/result.txt"
        result.unlink()
        self.assertIn("keep work for retry or repair", self.cli("clean-work", "--delete", "--task", "a"))
        self.assertTrue(previous.exists())
        self.run_complete()
        self.cli("clean-work", "--delete", "--task", "a")
        result.unlink()
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.run_complete()
        self.assertNotEqual(next((self.work / "work/a").iterdir()), previous)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1})

    def test_substituted_workspace_is_refused_and_incidental_symlink_is_not_followed(self):
        self.run_complete()
        workspace = next((self.work / "work/a").iterdir())
        external = self.work / "external"
        external.mkdir()
        (external / "keep.txt").write_text("keep")
        moved = workspace.with_name("saved")
        workspace.rename(moved)
        workspace.symlink_to(external, target_is_directory=True)
        self.assertIn("symlink", self.cli("clean-work", "--delete", "--task", "a", success=False))
        self.assertEqual((external / "keep.txt").read_text(), "keep")
        workspace.unlink()
        moved.rename(workspace)
        (workspace / "scratch-link").symlink_to(external, target_is_directory=True)
        self.cli("clean-work", "--delete", "--task", "a")
        self.assertFalse(workspace.exists())
        self.assertEqual((external / "keep.txt").read_text(), "keep")

    def test_failed_work_is_kept_while_completed_sibling_is_cleaned(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf b", "exit 1; printf b"))
        self.cli("run")
        self.settle()
        output = self.cli("clean-work", "--delete")
        self.assertIn("keep work for retry or repair", output)
        self.assertFalse(list((self.work / "work/a").iterdir()))
        self.assertTrue(list((self.work / "work/b").iterdir()))

    def test_active_consumer_does_not_block_completed_producer_work_cleanup(self):
        held, release = self.work / "consumer-held", self.work / "consumer-release"
        wait = f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
        (self.work / "workflow.py").write_text(self.with_consumer.replace("echo c", wait + "echo c"))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            self.cli("clean-work", "--delete", "--task", "a")
            self.assertFalse(list((self.work / "work/a").iterdir()))
            self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        finally:
            release.touch()
        self.finish()
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "a")

    def test_uncertain_submission_refuses_deletion(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="a__compute"), success=False)
        self.settle()
        self.assertIn("unresolved submission", self.cli("clean-work"))
        self.assertIn("unresolved submission", self.cli("clean-work", "--delete", success=False))
        self.assertTrue(list((self.work / "work/a").iterdir()))

    def test_preview_does_not_authorize_deleting_work_reported_active_later(self):
        self.run_complete()
        self.assertIn("eligible", self.cli("clean-work", "--task", "a"))
        output = self.cli("-b", "recovery_fixture", "clean-work", "--delete", "--task", "a",
                          env=self.inject(queued_prefix="a__compute"))
        self.assertIn("active work", output)
        self.assertTrue(list((self.work / "work/a").iterdir()))

    def test_cleanup_and_submission_serialize_through_removal(self):
        self.run_complete()
        previous = next((self.work / "work/a").iterdir())
        cleaner = subprocess.Popen([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "gate_cleanup", "a", "clean-work", "--delete", "--task", "a"],
                                   cwd=self.work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        runner = None
        try:
            self.wait_for((self.work / "cleanup-held").exists)
            runner = subprocess.Popen([GWF, "run", "--force-task", "a"], cwd=self.work,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.communicate(timeout=0.2)
            self.assertTrue(previous.exists())
            (self.work / "cleanup-release").touch()
            cleaned = cleaner.communicate(timeout=20)
            self.assertEqual(cleaner.returncode, 0, cleaned)
            submitted = runner.communicate(timeout=20)
            self.assertEqual(runner.returncode, 0, submitted)
        finally:
            (self.work / "cleanup-release").touch()
            for process in (cleaner, runner):
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=10)
        self.finish()
        self.assertFalse(previous.exists())
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1})

    def test_pending_cleanup_prevents_repair_using_not_yet_removed_work(self):
        self.run_complete()
        previous = next((self.work / "work/a").iterdir())
        outcome = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "cleanup_before_remove", "a", "clean-work", "--delete", "--task", "a"],
                                 cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(outcome.returncode, 102, outcome.stdout + outcome.stderr)
        self.assertTrue(previous.exists())
        (self.work / "results/a/result.txt").unlink()
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.run_complete()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1})

    def test_results_changed_after_cleanup_intent_keep_the_remaining_work(self):
        self.run_complete()
        workspace = next((self.work / "work/a").iterdir())
        cleaner = subprocess.Popen([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "gate_cleanup", "a", "clean-work", "--delete", "--task", "a"],
                                   cwd=self.work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for((self.work / "cleanup-held").exists)
            (self.work / "results/a/result.txt").unlink()
            (self.work / "cleanup-release").touch()
            output = cleaner.communicate(timeout=20)
            self.assertEqual(cleaner.returncode, 0, output)
            self.assertIn("keep work", "".join(output))
            self.assertTrue(workspace.exists())
        finally:
            (self.work / "cleanup-release").touch()
            if cleaner.poll() is None:
                cleaner.terminate()
                cleaner.communicate(timeout=10)

    def test_unrecorded_staging_sibling_is_not_claimed_by_its_uuid_name(self):
        self.run_complete()
        attempt = next((self.work / "work/a").iterdir()).name
        unrelated = self.work / ".gwf/gwflow/staging" / attempt / ("f" * 32)
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("unowned")
        self.cli("clean-work", "--delete", "--task", "a")
        self.assertEqual((unrelated / "keep.txt").read_text(), "unowned")

    def test_cleanup_interruptions_resume_without_repeating_computation(self):
        self.run_complete()
        for index, phase in enumerate(("cleanup_before_remove", "cleanup_during_remove", "cleanup_after_workspace_remove", "cleanup_before_completion")):
            with self.subTest(phase=phase):
                if index:
                    self.cli("run", "--force-task", "a")
                    self.finish()
                workspace = next(path for path in (self.work / "work/a").iterdir())
                outcome = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "a", "clean-work", "--delete", "--task", "a"],
                                         cwd=self.work, capture_output=True, text=True, timeout=30)
                self.assertIn(outcome.returncode, (102, 103, 104, 105), outcome.stdout + outcome.stderr)
                self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
                self.assertNotIn("Submitted target", self.cli("run"))
                self.cli("clean-work", "--delete", "--task", "a")
                self.assertFalse(workspace.exists())
                self.assertIn("already removed", self.cli("clean-work", "--task", "a"))
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 4, "b": 1})


class TransferStagingCleanupTests(LocalBackendTestCase):
    configure_workflow = test_transfer.TransferRecoveryTests.configure_workflow
    assert_results = test_transfer.TransferRecoveryTests.assert_results
    settle = test_transfer.TransferRecoveryTests.settle
    inject = test_transfer.TransferRecoveryTests.inject
    fault = test_transfer.TransferRecoveryTests.fault
    separate_filesystem = test_transfer.TransferRecoveryTests.separate_filesystem
    attempt = test_transfer.TransferRecoveryTests.attempt

    def test_completed_cleanup_removes_owned_transfer_leftovers_on_configured_storage(self):
        remote = self.separate_filesystem()
        self.configure_workflow(settings=f"results_root={str(remote / 'results')!r}, results_staging_root={str(remote / 'staging')!r}")
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_before_transfer_ownership=True))
        self.settle()
        self.cli("run")
        self.settle()
        result = remote / "results/samples/a/report"
        self.assert_results(result)
        preview = self.cli("clean-work")
        self.assertIn("Staging: " + str(remote / "staging"), preview)
        leftover = [path for path in (remote / "staging").rglob('*') if path.is_dir() and len(path.relative_to(remote / 'staging').parts) >= 2]
        self.assertTrue(leftover)
        self.cli("clean-work", "--delete")
        self.assertTrue(all(not path.exists() for path in leftover))
        self.assert_results(result)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_staging_directory_ownership_publication_interruptions_recover(self):
        for index, phase in enumerate(("crash_before_staging_ownership", "crash_after_staging_ownership", "crash_after_staging_directory_install")):
            with self.subTest(phase=phase):
                force = ("--force-task", "a") if index else ()
                self.cli("-b", "recovery_fixture", "run", *force, env=self.fault(**{phase: True}))
                self.settle()
                before = self.attempt()
                self.assertRegex(self.cli("explain"), r"Task a\s+Finish\s+")
                self.cli("run")
                self.settle()
                self.assertEqual(self.attempt(), before)
                self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
                self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"] * (index + 1))
                self.cli("clean-work", "--delete", "--task", "a")
                self.assertNotIn("Submitted target", self.cli("run"))
