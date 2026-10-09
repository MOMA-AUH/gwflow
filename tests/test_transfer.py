"""Interrupted retained-result transfer through real scheduled CLI jobs."""

import json
import shlex
import shutil

from support import FIXTURES, LocalBackendTestCase
import test_storage


class TransferRecoveryTests(LocalBackendTestCase):
    configure_workflow = test_storage.StoragePlacementTests.configure_workflow
    assert_results = test_storage.StoragePlacementTests.assert_results
    settle = test_storage.StoragePlacementTests.settle
    inject = test_storage.StoragePlacementTests.inject
    separate_filesystem = test_storage.StoragePlacementTests.separate_filesystem

    def fault(self, **options):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps(options))
        return self.inject(job_fault="a__gwflow_complete")

    def attempt(self):
        return next(line for line in self.cli("explain", "--details", "a").splitlines() if "Attempt:" in line)

    def test_partial_copy_restarts_without_repeating_computation(self):
        self.copy_interruption("crash_during_copy")

    def test_interruption_before_manifest_restarts_checked_copy(self):
        self.copy_interruption("crash_before_manifest")

    def test_interruption_before_transfer_ownership_can_allocate_new_staging(self):
        self.copy_interruption("crash_before_transfer_ownership")

    def copy_interruption(self, option):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(**{option: True}))
        self.settle()
        before = self.attempt()
        self.assertFalse((self.work / "results/samples/a/report").exists())
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        root = self.work / ".gwf/gwflow"
        snapshot = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
        self.assertRegex(self.cli("explain"), r"Task a\s+Finish\s+")
        self.assertIn("Would submit a__gwflow_complete", self.cli("run", "--dry-run", "--details"))
        self.assertEqual({str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}, snapshot)
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual(self.attempt(), before)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.assert_results(self.work / "results/samples/a/report")

    def test_prepared_transfer_installs_without_repeating_computation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_manifest=True))
        self.settle()
        before = self.attempt()
        self.assertFalse((self.work / "results/samples/a/report").exists())
        self.assertIn("install prepared", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual(self.attempt(), before)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.assert_results(self.work / "results/samples/a/report")

    def test_installed_transfer_finishes_completion_after_work_removal(self):
        self.installed_interruption("crash_after_results_install")

    def test_interruption_before_completion_rechecks_installed_set(self):
        self.installed_interruption("crash_before_completion")

    def installed_interruption(self, option):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(**{option: True}))
        self.settle()
        before = self.attempt()
        result = self.work / "results/samples/a/report"
        self.assert_results(result)
        identity = result.stat().st_ino
        shutil.rmtree(self.work / "work")
        self.assertIn("finish checked installed", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual(self.attempt(), before)
        self.assertEqual(result.stat().st_ino, identity)
        self.assertFalse((self.work / "work").exists())
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_incomplete_manifest_rebuilds_owned_results_from_checked_work(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        before = self.attempt()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").write_text("damaged result")
        (result / "unexpected.txt").write_text("not a retained output")
        manifest = next((self.work / ".gwf/gwflow").rglob("manifest.json"))
        record = json.loads(manifest.read_text())
        del record["outputs"]
        manifest.write_text(json.dumps(record))
        self.assertIn("rebuild owned", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual(self.attempt(), before)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.assert_results(result)

    def test_consumer_waits_for_recovered_complete_result_set(self):
        self.add_consumer()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        before = self.attempt()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.assertFalse((self.work / "results/c").exists())
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task c\s+Reuse\s+")
        self.assertEqual(self.attempt(), before)
        self.assertEqual((self.work / "results/c/joined.txt").read_text(), "firstsecond")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "consumer"])

    def add_consumer(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("gwf.task(task, alias='a'", "a = gwf.task(task, alias='a'") +
                            "from gwflow import shell\n"
                            "consumer = empty_task(inputs=[a.outputs['one'], a.outputs['two']])\n"
                            "target = consumer.target('join', inputs=consumer.inputs, outputs=['joined.txt'])\n"
                            f"target << shell({'cat {first} {second} > {out}; echo consumer >> ' + shlex.quote(str(self.work / 'trace'))!r}, first=a.outputs['one'], second=a.outputs['two'], out=target.output('joined.txt'))\n"
                            "consumer.retain('joined', source=target.output('joined.txt'), path='joined.txt')\n"
                            "gwf.task(consumer, alias='c')\n")

    def test_queued_consumer_protects_installed_set_even_when_declaration_removed(self):
        self.add_consumer()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").write_text("damaged but protected")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().split("from gwflow import shell")[0])
        env = self.inject(queued_prefix="c__gwflow_prepare")
        self.assertIn("Active consumers block replacement: c", self.cli("-b", "recovery_fixture", "run", env=env, success=False))
        self.assertEqual((result / "renamed.txt").read_text(), "damaged but protected")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.cli("run")
        self.settle()
        self.assert_results(result)

    def test_rejected_recovery_still_checks_queued_consumers_before_resubmission(self):
        self.add_consumer()
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").write_text("damaged but protected")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().split("from gwflow import shell")[0])
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        env = self.inject(queued_prefix="c__gwflow_prepare")
        self.assertIn("Active consumers block replacement: c", self.cli("-b", "recovery_fixture", "run", env=env, success=False))
        self.assertEqual((result / "renamed.txt").read_text(), "damaged but protected")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.cli("run")
        self.settle()
        self.assert_results(result)

    def test_missing_transfer_ownership_leaves_installed_results_untouched(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        ownership = next((self.work / ".gwf/gwflow").rglob("transfer.json"))
        original = ownership.read_bytes()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").write_text("preserve this damaged set")
        for damaged in (None, b'{"kind":', b'{}'):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    ownership.unlink()
                else:
                    ownership.write_bytes(damaged)
                self.assertIn("ownership", self.cli("run", success=False))
                self.assertEqual((result / "renamed.txt").read_text(), "preserve this damaged set")
                self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        ownership.write_bytes(original)
        self.cli("run")
        self.settle()
        self.assert_results(result)

    def test_empty_retained_set_recovers_installed_directory(self):
        workflow = self.work / "workflow.py"
        workflow.write_text('\n'.join(line for line in workflow.read_text().splitlines() if not line.startswith("task.retain(")) + '\n')
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        before = self.attempt()
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual(self.attempt(), before)
        self.assertEqual(list((self.work / "results/samples/a/report").iterdir()), [])
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_unacknowledged_interrupted_transfer_blocks_duplicate_copy(self):
        env = self.fault(crash_during_copy=True)
        self.inject(job_fault="a__gwflow_complete", lose_tracking="a__gwflow_complete")
        self.cli("-b", "recovery_fixture", "run", env=env, success=False)
        self.settle()
        self.assertIn("submission outcome unknown", self.cli("explain"))
        self.assertIn("unresolved submission", self.cli("run", success=False))
        self.assertFalse((self.work / "results/samples/a/report").exists())
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_damaged_partial_transfer_source_requires_new_computation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        before = self.attempt()
        next((self.work / "work").rglob("one.txt")).write_text("invalid source")
        self.assertRegex(self.cli("explain"), r"Task a\s+Retry\s+")
        self.cli("run")
        self.settle()
        self.assertEqual(self.attempt(), before)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])
        self.assert_results(self.work / "results/samples/a/report")

    def test_unusable_installed_transfer_and_sources_start_fresh_attempt(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        before = self.attempt()
        next((self.work / ".gwf/gwflow").rglob("manifest.json")).write_text('{}')
        shutil.rmtree(self.work / "work")
        self.assertRegex(self.cli("explain"), r"Task a\s+Run\s+")
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.attempt(), before)
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])
        self.assert_results(self.work / "results/samples/a/report")

    def test_partial_copy_recovers_on_distinct_results_filesystem(self):
        remote = self.separate_filesystem()
        self.configure_workflow(settings=f"results_root={str(remote / 'results')!r}, results_staging_root={str(remote / 'staging')!r}")
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        before = self.attempt()
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual(self.attempt(), before)
        self.assert_results(remote / "results/samples/a/report")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_installed_coarse_metadata_recovery_uses_destination_observations(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(coarse_mtime=True, crash_after_results_install=True))
        self.settle()
        result = self.work / "results/samples/a/report"
        self.assertEqual((result / "renamed.txt").stat().st_mtime_ns, 946684800000000000)
        shutil.rmtree(self.work / "work")
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual((result / "renamed.txt").stat().st_mtime_ns, 946684800000000000)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_extra_installed_symlink_requires_owned_rebuild_without_following_it(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_after_results_install=True))
        self.settle()
        result = self.work / "results/samples/a/report"
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("untouched")
        (result / "extra").symlink_to(outside, target_is_directory=True)
        self.assertIn("rebuild owned", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assert_results(result)
        self.assertFalse((result / "extra").exists())
        self.assertEqual((outside / "sentinel").read_text(), "untouched")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_missing_unretained_intermediate_does_not_repeat_verified_computation(self):
        workflow = self.work / "workflow.py"
        workflow.write_text('\n'.join(line for line in workflow.read_text().splitlines() if not line.startswith("task.retain('two'")) + '\n')
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_during_copy=True))
        self.settle()
        next((self.work / "work").rglob("two.txt")).unlink()
        self.assertRegex(self.cli("explain"), r"Task a\s+Finish\s+")
        self.cli("run")
        self.settle()
        self.assertRegex(self.cli("explain"), r"Task a\s+Reuse\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.assertEqual((self.work / "results/samples/a/report/renamed.txt").read_text(), "first")
