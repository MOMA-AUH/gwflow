"""Repair retained results through the installed CLI and scheduled workers."""

import os
import shutil
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_transfer


class ResultsRepairTests(LocalBackendTestCase):
    configure_workflow = test_transfer.TransferRecoveryTests.configure_workflow
    assert_results = test_transfer.TransferRecoveryTests.assert_results
    settle = test_transfer.TransferRecoveryTests.settle
    inject = test_transfer.TransferRecoveryTests.inject
    fault = test_transfer.TransferRecoveryTests.fault
    attempt = test_transfer.TransferRecoveryTests.attempt
    add_consumer = test_transfer.TransferRecoveryTests.add_consumer
    separate_filesystem = test_transfer.TransferRecoveryTests.separate_filesystem

    def finishing_generation(self):
        line = next(line for line in self.cli("explain", "--details", "a").splitlines() if "a__gwflow_complete:" in line)
        return line.split(": ")[1].split()[0].rsplit("__", 1)[0]

    def test_missing_result_repairs_same_attempt_and_preserves_consumer_reuse(self):
        self.add_consumer()
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report"
        original = (result / "renamed.txt").stat()
        (result / "renamed.txt").unlink()
        root = self.work / ".gwf/gwflow"
        snapshot = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
        preview = self.cli("explain")
        self.assertRegex(preview, r"Task a\s+Repair\s+")
        self.assertRegex(preview, r"Task c\s+Defer\s+")
        self.assertIn("run again after upstream result recovery", preview)
        self.cli("run", "--dry-run")
        self.assertEqual({str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}, snapshot)
        self.assertIn("run again after upstream result recovery", self.cli("run"))
        self.finish()
        self.assertEqual(self.attempt(), before)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"])
        self.assertEqual((result / "renamed.txt").stat().st_mtime_ns, original.st_mtime_ns)
        self.assert_results(result)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertRegex(self.cli("explain"), r"Task c\s+Reuse\s+")
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_missing_result_directory_is_repaired_without_computation(self):
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report"
        shutil.rmtree(result)
        self.assertRegex(self.cli("explain"), r"Task a\s+Repair\s+")
        self.run_complete()
        self.assertEqual(self.attempt(), before)
        self.assert_results(result)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_manual_edits_and_mtime_changes_are_overwritten_by_repair(self):
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report"
        path = result / "renamed.txt"
        original = path.stat()
        for damage in ("size", "mtime"):
            with self.subTest(damage=damage):
                if damage == "size":
                    path.write_text("manual edit")
                    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                else:
                    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1))
                self.assertRegex(self.cli("explain"), r"Task a\s+Repair\s+")
                self.run_complete()
                self.assertEqual(self.attempt(), before)
                self.assert_results(result)
                self.assertEqual(path.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_invalid_work_sources_require_fresh_producer_and_consumer(self):
        self.add_consumer()
        self.run_complete()
        for damage in ("missing", "corrupt", "evidence", "removed-work"):
            with self.subTest(damage=damage):
                before = self.attempt()
                (self.work / "results/samples/a/report/renamed.txt").unlink()
                current = [path for path in (self.work / "work/a").rglob("one.txt") if before.split()[-1] in str(path)][0]
                if damage == "missing":
                    current.unlink()
                elif damage == "corrupt":
                    current.write_text("invalid source")
                elif damage == "evidence":
                    success = [path for path in (self.work / ".gwf/gwflow").rglob("success.json") if before.split()[-1] in str(path)][0]
                    success.write_text('{}')
                else:
                    shutil.rmtree(self.work / "work")
                preview = self.cli("explain")
                self.assertRegex(preview, r"Task a\s+Run\s+")
                self.assertRegex(preview, r"Task c\s+Run\s+")
                self.run_complete()
                self.assertNotEqual(self.attempt(), before)
                self.assertRegex(self.cli("explain"), r"Task c\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"] * 5)

    def test_partial_repair_keeps_old_results_until_full_replacement_is_ready(self):
        self.add_consumer()
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").write_text("damaged retained set")
        previous = self.finishing_generation()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        self.assertEqual((result / "renamed.txt").read_text(), "damaged retained set")
        self.assertEqual((result / "nested/two.txt").read_text(), "second")
        self.assertEqual(self.attempt(), before)
        detail = self.cli("explain", "--details", "a")
        selected = self.finishing_generation()
        self.assertNotEqual(previous, selected)
        self.assertRegex(detail, r"Task a\s+Finish\s+")
        self.assertRegex(self.cli("explain"), r"Task c\s+Defer\s+")
        self.cli("run")
        self.settle()
        self.assert_results(result)
        self.assertEqual(self.finishing_generation(), selected)
        self.assertRegex(self.cli("explain"), r"Task c\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"])

    def test_restored_timestamp_precision_defers_then_invalidates_consumer(self):
        self.add_consumer()
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report/renamed.txt"
        original = result.stat().st_mtime_ns
        result.unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(coarse_mtime=True))
        self.finish()
        self.assertEqual(self.attempt(), before)
        self.assertEqual(result.stat().st_mtime_ns, original // 1_000_000_000 * 1_000_000_000)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"])
        preview = self.cli("explain")
        self.assertRegex(preview, r"Task a\s+Reuse\s+")
        self.assertRegex(preview, r"Task c\s+Run\s+")
        self.run_complete()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer", "consumer"])

    def test_missing_results_root_is_recreated_before_repair(self):
        self.run_complete()
        before = self.attempt()
        shutil.rmtree(self.work / "results")
        self.assertRegex(self.cli("explain"), r"Task a\s+Repair\s+")
        self.run_complete()
        self.assertEqual(self.attempt(), before)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assert_results(self.work / "results/samples/a/report")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_repair_frontend_interruptions_preserve_results_and_can_resume(self):
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report/renamed.txt"
        for phase in ("before_repair_intent", "before_repair_selection", "after_repair_selection"):
            with self.subTest(phase=phase):
                result.write_text("damaged before repair")
                outcome = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "a", "run"],
                                         cwd=self.work, capture_output=True, text=True, timeout=30)
                self.assertIn(outcome.returncode, (99, 100, 101), outcome.stdout + outcome.stderr)
                self.assertEqual(result.read_text(), "damaged before repair")
                self.assertEqual(self.attempt(), before)
                self.run_complete()
                self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
                self.assertEqual(result.read_text(), "first")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_repair_installation_interruptions_resume_same_operation(self):
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report"
        for phase in ("crash_before_installation_intent", "crash_after_installation_intent", "crash_during_results_removal",
                      "crash_after_results_removal", "crash_after_results_install", "crash_before_completion"):
            with self.subTest(phase=phase):
                (result / "renamed.txt").write_text("damaged before replacement")
                self.cli("-b", "recovery_fixture", "run", env=self.fault(**{phase: True}))
                self.settle()
                selected = self.finishing_generation()
                if phase in ("crash_before_installation_intent", "crash_after_installation_intent"):
                    self.assertEqual((result / "renamed.txt").read_text(), "damaged before replacement")
                    self.assertEqual((result / "nested/two.txt").read_text(), "second")
                self.assertRegex(self.cli("explain"), r"Task a\s+Finish\s+")
                self.cli("run")
                self.settle()
                self.assertEqual(self.finishing_generation(), selected)
                self.assertEqual(self.attempt(), before)
                self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
                self.assert_results(result)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_queued_consumer_blocks_repair_after_its_declaration_is_removed(self):
        self.add_consumer()
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report/renamed.txt"
        result.write_text("protected manual edit")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().split("from gwflow import shell")[0])
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                if rejected:
                    self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
                env = self.inject(queued_prefix="c__join")
                self.assertIn("Active consumers block replacement: c", self.cli("-b", "recovery_fixture", "run", env=env, success=False))
                self.assertEqual(result.read_text(), "protected manual edit")
                self.assertEqual(self.attempt(), before)
        self.run_complete()
        self.assertEqual(result.read_text(), "first")

    def test_uncertain_consumer_blocks_repair(self):
        self.run_complete()
        self.add_consumer()
        env = self.fault(crash_before_commit=True)
        self.inject(job_fault="c__join", lose_tracking="c__join")
        self.cli("-b", "recovery_fixture", "run", env=env, success=False)
        self.settle()
        result = self.work / "results/samples/a/report/renamed.txt"
        result.write_text("protected after lost acknowledgement")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().split("from gwflow import shell")[0])
        self.assertIn("Active consumers block replacement: c", self.cli("run", success=False))
        self.assertEqual(result.read_text(), "protected after lost acknowledgement")

    def test_new_consumer_waits_for_repaired_complete_set(self):
        self.run_complete()
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.add_consumer()
        self.run_complete()
        self.assertRegex(self.cli("explain"), r"Task c\s+Reuse\s+")
        self.assertEqual((self.work / "results/c/joined.txt").read_text(), "firstsecond")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"])

    def test_command_tracking_governs_repair_eligibility(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        before = self.attempt()
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        workflow.write_text(original.replace("printf first", "printf FIRST_CHANGED"))
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.configure(use_spec_hashes=False)
        self.assertRegex(self.cli("explain"), r"Task a\s+Repair\s+")
        self.run_complete()
        self.assertEqual(self.attempt(), before)
        self.assert_results(self.work / "results/samples/a/report")
        self.configure(use_spec_hashes=True)
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.run_complete()
        self.assertNotEqual(self.attempt(), before)
        self.assertEqual((self.work / "results/samples/a/report/renamed.txt").read_text(), "FIRST_CHANGED")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])

    def test_installed_repair_can_complete_after_sources_are_removed(self):
        self.run_complete()
        before = self.attempt()
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        shutil.rmtree(self.work / "work")
        self.assertIn("finish checked installed", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertEqual(self.attempt(), before)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertFalse((self.work / "work").exists())

    def test_lost_sources_during_interrupted_repair_require_fresh_attempt(self):
        self.run_complete()
        before = self.attempt()
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        next((self.work / "work").rglob("one.txt")).unlink()
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt(), before)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])

    def test_missing_selected_repair_intent_blocks_recovery(self):
        self.run_complete()
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        intent = next((self.work / ".gwf/gwflow").rglob("repair.json"))
        original = intent.read_bytes()
        intent.unlink()
        self.assertIn("repair intent", self.cli("run", success=False))
        self.assertFalse((self.work / "results/samples/a/report/renamed.txt").exists())
        intent.write_bytes(original)
        self.run_complete()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")

    def test_interrupted_rebuild_of_damaged_installed_repair_retains_ownership(self):
        self.run_complete()
        before = self.attempt()
        result = self.work / "results/samples/a/report/renamed.txt"
        result.unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        selected = self.finishing_generation()
        result.write_text("damaged during interrupted repair")
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        self.assertEqual(result.read_text(), "damaged during interrupted repair")
        self.assertRegex(self.cli("explain"), r"Task a\s+Finish\s+")
        self.cli("run")
        self.settle()
        self.assertEqual(self.attempt(), before)
        self.assertEqual(self.finishing_generation(), selected)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual(result.read_text(), "first")

    def test_repair_on_separate_filesystem_recreates_missing_staging_root(self):
        remote = self.separate_filesystem()
        self.configure_workflow(settings=f"results_root={str(remote / 'results')!r}, results_staging_root={str(remote / 'staging')!r}")
        self.run_complete()
        before = self.attempt()
        result = remote / "results/samples/a/report"
        (result / "renamed.txt").unlink()
        shutil.rmtree(remote / "staging")
        self.run_complete()
        self.assertEqual(self.attempt(), before)
        self.assert_results(result)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_selected_repair_does_not_remove_substituted_result_directory(self):
        self.run_complete()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        result.rename(result.with_name("original"))
        result.mkdir()
        (result / "unrelated.txt").write_text("unowned")
        self.assertIn("ownership", self.cli("run", success=False))
        self.assertEqual((result / "unrelated.txt").read_text(), "unowned")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_active_repair_defers_existing_consumer_and_allows_independent_work(self):
        self.add_consumer()
        self.run_complete()
        before = self.attempt()
        (self.work / "results/samples/a/report/renamed.txt").unlink()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_manifest=True))
        self.wait_for((self.work / "manifest-held").exists)
        try:
            workflow = self.work / "workflow.py"
            workflow.write_text(workflow.read_text() + "gwf.task_from_template('b', task)\n")
            preview = self.cli("explain")
            self.assertRegex(preview, r"Task a\s+Wait\s+")
            self.assertRegex(preview, r"Task c\s+Defer\s+")
            self.assertIn("run again after upstream result recovery", preview)
            self.assertIn("Submitted target b__compute", self.cli("run"))
            self.wait_for((self.work / "results/b/renamed.txt").exists)
            self.assertFalse((self.work / "results/samples/a/report/renamed.txt").exists())
            self.assertEqual((self.work / "results/c/joined.txt").read_text(), "firstsecond")
        finally:
            (self.work / "manifest-release").touch()
        self.finish()
        self.assertEqual(self.attempt(), before)
        self.assertRegex(self.cli("explain"), r"Task c\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer", "compute"])
