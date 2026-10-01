"""Storage placement through installed authoring, CLI, and real filesystems."""

import os
import json
from pathlib import Path
import shlex
import shutil
import tempfile

from support import FIXTURES, LocalBackendTestCase
import test_managed
import test_managed_recovery


class StoragePlacementTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject

    def configure_workflow(self, settings="", result_dir="samples/a/report"):
        trace = shlex.quote(str(self.work / "trace"))
        command = (f"echo compute >> {trace}; mkdir -p nested; printf first > nested/one.txt; "
                   "printf second > two.txt; printf scratch > scratch.txt; "
                   "touch -m -d @946684800.123456789 nested/one.txt two.txt")
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow\n"
            f"gwf = Workflow({settings})\n"
            "task = Task(inputs=[])\n"
            "target = task.target('compute', inputs=[], outputs=['nested/one.txt', 'two.txt'])\n"
            f"target << {command!r}\n"
            "task.retain('one', source=target.output('nested/one.txt'), path='renamed.txt')\n"
            "task.retain('two', source=target.output('two.txt'), path='nested/two.txt')\n"
            f"gwf.task_from_template('a', task, result_dir={result_dir!r})\n"
        )

    def separate_filesystem(self):
        candidate = Path('/dev/shm')
        if not candidate.is_dir() or candidate.stat().st_dev == self.work.stat().st_dev:
            self.skipTest("A writable separate filesystem is unavailable")
        try:
            temporary = tempfile.TemporaryDirectory(prefix="gwflow storage '; ", dir=candidate)
        except OSError as error:
            self.skipTest(f"A writable separate filesystem is unavailable: {error}")
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def assert_results(self, root):
        self.assertEqual((root / "renamed.txt").read_text(), "first")
        self.assertEqual((root / "nested/two.txt").read_text(), "second")
        self.assertEqual(sorted(str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()),
                         ["nested/two.txt", "renamed.txt"])

    def test_worker_device_numbers_do_not_change_shared_directory_ownership(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"device_offset": 10000}))
        env = self.inject(job_fault="a")
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.settle()
        result = self.work / "results/samples/a/report"
        self.assertTrue(result.exists(), self.cli("logs", "a__gwflow_prepare", "--stderr", "--no-pager"))
        self.assert_results(result)
        self.assertNotIn("Submitted target", self.cli("run"))
        (result / "renamed.txt").unlink()
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.finish()
        self.assert_results(result)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.cli("clean-work", "--delete")
        self.assertNotIn("Submitted target", self.cli("run"))
        shutil.rmtree(self.work / "work")
        self.cli("-b", "recovery_fixture", "run", "--force", env=env)
        self.finish()
        self.assert_results(result)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])

    def test_legacy_roots_upgrade_before_repair_on_another_worker(self):
        self.run_complete()
        path = self.work / ".gwf/gwflow/owner.json"
        owner = json.loads(path.read_text())
        for key, witness in owner.pop("root_witnesses").items():
            (Path(owner["locations"][key]) / witness["name"]).rmdir()
        path.write_text(json.dumps(owner))
        before = path.read_bytes()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").unlink()
        self.cli("run", "--dry-run")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.work.rglob(".gwflow-root-*")))
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"device_offset": 10000}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="a"))
        self.finish()
        self.assert_results(result)
        upgraded = json.loads(path.read_text())
        self.assertEqual(upgraded["root_identity"], owner["root_identity"])
        self.assertEqual(upgraded["root_witnesses"].keys(), owner["locations"].keys())
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_changed_root_witness_blocks_mutation_without_adopting_storage(self):
        self.run_complete()
        owner_path = self.work / ".gwf/gwflow/owner.json"
        owner = json.loads(owner_path.read_text())
        witness = self.work / "results" / owner["root_witnesses"]["results"]["name"]
        saved = witness.with_name("saved-witness")
        witness.rename(saved)
        result = self.work / "results/samples/a/report"
        for replacement in ("missing", "new directory", "symlink"):
            with self.subTest(replacement=replacement):
                if replacement == "new directory":
                    witness.mkdir()
                elif replacement == "symlink":
                    witness.symlink_to(saved, target_is_directory=True)
                output = self.cli("run", "--force", success=False)
                self.assertRegex(output, "identity changed|symlink")
                self.assert_results(result)
                if witness.is_symlink():
                    witness.unlink()
                elif witness.exists():
                    witness.rmdir()
        saved.rename(witness)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_copied_root_with_witness_is_not_adopted(self):
        self.run_complete()
        original, saved = self.work / "results", self.work / "saved-results"
        original.rename(saved)
        shutil.copytree(saved, original)
        self.assertIn("Managed root identity changed", self.cli("run", "--force", success=False))
        self.assert_results(original / "samples/a/report")
        self.assert_results(saved / "samples/a/report")

    def test_work_on_separate_filesystem_keeps_sources_and_uses_default_staging(self):
        remote = self.separate_filesystem()
        self.configure_workflow(settings=f"work_root={str(remote / 'scratch')!r}")
        self.run_complete()
        result = self.work / "results/samples/a/report"
        self.assert_results(result)
        source = next((remote / "scratch").rglob("one.txt"))
        self.assertNotEqual(source.stat().st_dev, (result / "renamed.txt").stat().st_dev)
        self.assertEqual(source.stat().st_mtime_ns, (result / "renamed.txt").stat().st_mtime_ns)
        self.assertEqual(source.read_text(), "first")
        shutil.rmtree(remote / "scratch")
        self.assertIn("Task a: reuse;", self.cli("explain"))
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assert_results(result)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])

    def test_remote_results_require_staging_on_their_filesystem(self):
        remote = self.separate_filesystem()
        result_root, staging = remote / "durable results", remote / "private staging"
        settings = f"results_root={str(result_root)!r}"
        for placement in (settings, settings + ", results_staging_root='local staging'"):
            with self.subTest(placement=placement):
                self.configure_workflow(settings=placement)
                self.assertIn("Results staging must share the results filesystem", self.cli("run", success=False))
                self.assertFalse((self.work / ".gwf/gwflow").exists())
                self.assertFalse(result_root.exists())
        self.configure_workflow(settings=settings + f", results_staging_root={str(staging)!r}")
        self.run_complete()
        self.assert_results(result_root / "samples/a/report")
        self.assertNotEqual((self.work / "work").stat().st_dev, result_root.stat().st_dev)
        self.assertEqual(staging.stat().st_dev, result_root.stat().st_dev)
        self.assertEqual([p.relative_to(result_root).as_posix() for p in result_root.rglob('*') if p.is_file()],
                         ["samples/a/report/renamed.txt", "samples/a/report/nested/two.txt"])
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_root_aliases_grouped_results_and_new_task_locations(self):
        roots = self.work / "roots with ' punctuation;"
        roots.mkdir()
        alias = self.work / "root-alias"
        alias.symlink_to(roots, target_is_directory=True)
        self.configure_workflow(settings="work_root='root-alias/scratch', results_root='root-alias/retained'")
        self.run_complete()
        result_root = roots / "retained"
        self.assert_results(result_root / "samples/a/report")
        self.configure_workflow(settings=f"work_root={str(roots / 'scratch')!r}, results_root={str(result_root)!r}")
        self.assertNotIn("Submitted target", self.cli("run"))
        with (self.work / "workflow.py").open('a') as stream:
            stream.write("gwf.task_from_template('b', task, result_dir='samples/b/report')\n")
        self.run_complete()
        self.assert_results(result_root / "samples/b/report")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])
        self.configure_workflow(settings=f"work_root={str(roots / 'scratch')!r}, results_root={str(result_root)!r}", result_dir="moved/a")
        self.assertIn("Recorded Task results location changed", self.cli("run", success=False))
        self.assertFalse((result_root / "moved").exists())
        self.assert_results(result_root / "samples/a/report")

    def test_staging_and_aliased_overlaps_fail_before_initialization(self):
        (self.work / "alias").symlink_to(self.work, target_is_directory=True)
        cases = ("work_root='alias/.gwf/scratch'", "results_root='alias/work/results'",
                 "results_staging_root='results/private'", "results_staging_root='work/private'",
                 "results_staging_root='.'", "results_staging_root='.gwf'")
        for settings in cases:
            with self.subTest(settings=settings):
                self.configure_workflow(settings=settings)
                self.cli("run", success=False)
                self.assertFalse((self.work / ".gwf/gwflow").exists())
                self.assertFalse((self.work / "work").exists())
                self.assertFalse((self.work / "results").exists())

    def test_same_task_declaration_runs_under_independent_initial_roots(self):
        self.configure_workflow(settings="work_root='first scratch', results_root='first durable'")
        self.run_complete()
        first = self.work
        second = first / "another pipeline"
        second.mkdir()
        source = (first / "workflow.py").read_text()
        (second / "workflow.py").write_text(source.replace("first scratch", "second scratch").replace("first durable", "second durable"))
        shutil.copy(first / ".gwfconf.json", second)
        try:
            self.work = second
            self.run_complete()
            self.assertNotIn("Submitted target", self.cli("run"))
        finally:
            self.work = first
        self.assert_results(first / "first durable/samples/a/report")
        self.assert_results(second / "second durable/samples/a/report")
        self.assertEqual((first / "trace").read_text().splitlines(), ["compute", "compute"])

    def test_runtime_staging_substitution_cannot_redirect_results_installation(self):
        staging = self.work / "private staging"
        self.configure_workflow(settings="results_staging_root='private staging'")
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_after_manifest": True}))
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("untouched")
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="a__gwflow_complete"))
        self.wait_for(lambda: (self.work / "manifest-held").exists())
        try:
            staging.rename(self.work / "saved-staging")
            staging.symlink_to(outside, target_is_directory=True)
        finally:
            (self.work / "manifest-release").touch()
        self.settle()
        self.assertFalse((self.work / "results/samples/a/report").exists())
        self.assertEqual([p.name for p in outside.iterdir()], ["sentinel"])
        self.assertEqual((outside / "sentinel").read_text(), "untouched")
        self.assertEqual(next((self.work / "work").rglob("one.txt")).read_text(), "first")

    def test_grouping_parents_cannot_be_symlinked_or_task_owned(self):
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        for result_dir in ("samples/a", "samples/a/report", "samples/a/report/nested"):
            with self.subTest(result_dir=result_dir):
                workflow.write_text(original + f"gwf.task_from_template('b', task, result_dir={result_dir!r})\n")
                self.cli("run", success=False)
                self.assertFalse((self.work / ".gwf/gwflow").exists())
        workflow.write_text(original)
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("untouched")
        (self.work / "results").mkdir()
        (self.work / "results/samples").symlink_to(outside, target_is_directory=True)
        self.assertIn("symlink", self.cli("run", success=False))
        self.assertEqual([p.name for p in outside.iterdir()], ["sentinel"])
        self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_unsupported_timestamp_preservation_records_actual_result_metadata(self):
        self.result_timestamp_observation("unsupported_mtime")

    def test_coarser_destination_timestamps_are_independent_of_work_observations(self):
        self.result_timestamp_observation("coarse_mtime")

    def result_timestamp_observation(self, option):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({option: True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="a__gwflow_complete"))
        self.finish()
        result = self.work / "results/samples/a/report"
        self.assert_results(result)
        source = next((self.work / "work").rglob("one.txt"))
        self.assertEqual(source.stat().st_mtime_ns, 946684800123456789)
        self.assertNotEqual(source.stat().st_mtime_ns, (result / "renamed.txt").stat().st_mtime_ns)
        if option == "coarse_mtime":
            self.assertEqual((result / "renamed.txt").stat().st_mtime_ns, 946684800000000000)
        shutil.rmtree(self.work / "work")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertIn("Task a: reuse;", self.cli("explain"))
