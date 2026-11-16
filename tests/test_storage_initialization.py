"""Initial storage recovery through installed commands and real local jobs."""

import subprocess
import sys

from gwf.backends.local import Client, get_socket_path

from support import FIXTURES, LocalBackendTestCase
import test_storage


class StorageInitializationTests(LocalBackendTestCase):
    configure_workflow = test_storage.StoragePlacementTests.configure_workflow
    staging = ".gwf/gwflow/staging"

    def fault_command(self, phase):
        return [sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "a", "run"]

    def interrupt(self, boundary, *, error=False):
        result = subprocess.run(
            self.fault_command("bootstrap_" + boundary + ("_error" if error else "_interrupt")),
            cwd=self.work, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout + result.stderr
        if error:
            self.assertEqual(result.returncode, 1, output)
            self.assertIn("injected bootstrap storage full", output)
            self.assertIn("Resolve the storage problem", output)
        else:
            self.assertEqual(result.returncode, 129, output)
            self.assertIn("Injected interruption", output)
        self.assertNoSubmissions(output)
        self.assert_submission_count(0)
        return result

    def assert_submission_count(self, count):
        with Client.connect(get_socket_path(self.work)) as client:
            self.assertEqual(len(client.status()), count)

    def recover(self, boundary, *, error=False):
        self.interrupt(boundary, error=error)
        self.cli("status", "--json")
        self.cli("run", "--dry-run")
        self.cli("clean", "--delete")
        self.assert_submission_count(0)
        self.check_completion()

    def check_completion(self):
        self.run_complete()
        attempt = self.task_attempts()["a"]
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        retained = self.work / "results/samples/a/report/renamed.txt"
        self.assertEqual(retained.read_text(), "first")
        self.assertNoSubmissions(self.cli("run"))
        self.assert_submission_count(3)
        self.cli("clean", "--delete")
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual(self.task_attempts()["a"], attempt)
        self.assertEqual(retained.read_text(), "first")
        self.assert_submission_count(3)

    def test_owner_publication_storage_error_can_restart(self):
        self.recover("owner", error=True)

    def test_private_allocation_interruption_can_restart(self):
        self.recover("allocation")

    def test_before_temporary_publication_interruption_can_restart(self):
        self.recover("before_temporary")

    def test_temporary_file_creation_interruption_can_restart(self):
        self.recover("temporary")

    def test_temporary_file_sync_error_can_restart(self):
        self.recover("temporary_sync", error=True)

    def test_before_owner_publication_interruption_can_restart(self):
        self.recover("owner")

    def test_managed_root_creation_interruption_can_restart(self):
        self.recover("root")

    def test_before_owner_installation_interruption_can_restart(self):
        self.recover("install")

    def test_owner_installation_error_can_restart(self):
        self.recover("install", error=True)

    def test_installed_owner_interruption_can_restart(self):
        self.recover("installed")

    def test_installed_owner_sync_error_can_restart(self):
        self.recover("installed_sync", error=True)

    def test_work_creation_interruption_can_restart(self):
        self.recover("work")

    def test_results_creation_interruption_can_restart(self):
        self.recover("results")

    def test_staging_creation_interruption_can_restart(self):
        self.recover("explicit-staging" if self.staging == "explicit-staging" else "staging")

    def test_without_fault_runs_and_reuses(self):
        self.check_completion()

    def assert_refused(self, diagnostic, protected):
        before = {path: path.read_bytes() for path in protected}
        for arguments in (("status", "--json"), ("run", "--dry-run"), ("run",), ("clean", "--delete")):
            with self.subTest(arguments=arguments):
                output = self.cli(*arguments, success=False)
                self.assertIn(diagnostic, output)
                self.assertNoSubmissions(output)
                self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assert_submission_count(0)

    def test_foreign_managed_data_after_interruption_is_preserved(self):
        self.interrupt("owner")
        root = self.work / ".gwf/gwflow"
        root.mkdir(exist_ok=True)
        foreign = root / "foreign.txt"
        foreign.write_text("preserve foreign data")
        self.assert_refused("ownership", [foreign])

    def test_unowned_staging_is_not_recognized_as_bootstrap_evidence(self):
        self.interrupt("owner")
        root = self.work / ".gwf/gwflow/staging"
        root.mkdir(parents=True, exist_ok=True)
        foreign = root / "foreign.txt"
        foreign.write_text("preserve foreign staging")
        self.assert_refused("ownership", [foreign])

    def test_malformed_owner_after_interruption_is_preserved(self):
        self.interrupt("installed")
        owner = self.work / ".gwf/gwflow/owner.json"
        owner.write_text("{incomplete")
        self.assert_refused("ownership", [owner])

    def test_changed_locations_after_interruption_are_refused(self):
        self.interrupt("installed")
        owner = self.work / ".gwf/gwflow/owner.json"
        self.configure_workflow(settings="work_root='another-work'")
        self.assert_refused("recorded storage locations", [owner])

    def test_substituted_staging_directory_is_refused(self):
        self.interrupt("installed")
        foreign = self.work / "foreign"
        foreign.mkdir()
        protected = foreign / "keep.txt"
        protected.write_text("preserve substituted directory")
        (self.work / self.staging).symlink_to(foreign, target_is_directory=True)
        self.assert_refused("symlink" if self.staging.startswith(".gwf/") else "recorded storage locations", [protected])

    def test_private_allocation_substitution_preserves_foreign_owner(self):
        process = subprocess.Popen(self.fault_command("bootstrap_private_gate"), cwd=self.work,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_worker, process)
        self.wait_for(lambda: (self.work / "bootstrap-held").exists())
        allocation = next((self.work / ".gwf").glob(".gwflow-bootstrap-*"))
        allocation.rename(self.work / ".gwf/displaced")
        allocation.mkdir()
        foreign = allocation / "owner.json"
        foreign.write_text("preserve foreign owner")
        (self.work / "bootstrap-release").touch()
        output = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0, output)
        self.assertIn("changed", "".join(output))
        self.assertEqual(foreign.read_text(), "preserve foreign owner")
        self.assertEqual(list(allocation.iterdir()), [foreign])
        self.assert_submission_count(0)
        self.check_completion()

    def test_managed_root_substitution_during_publication_is_refused(self):
        process = subprocess.Popen(self.fault_command("bootstrap_gate"), cwd=self.work,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_worker, process)
        self.wait_for(lambda: (self.work / "bootstrap-held").exists())
        root = self.work / ".gwf/gwflow"
        root.rename(self.work / ".gwf/displaced")
        root.mkdir()
        foreign = root / "foreign.txt"
        foreign.write_text("preserve substituted root")
        (self.work / "bootstrap-release").touch()
        output = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0, output)
        self.assertIn("changed", "".join(output))
        self.assertEqual(foreign.read_text(), "preserve substituted root")
        self.assertEqual(list(root.iterdir()), [foreign])
        self.assert_submission_count(0)

    def test_concurrent_initial_runs_admit_one_attempt(self):
        first = subprocess.Popen(self.fault_command("bootstrap_gate"), cwd=self.work,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_worker, first)
        self.wait_for(lambda: (self.work / "bootstrap-held").exists())
        second = subprocess.Popen([self.executable, "run"], cwd=self.work,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_worker, second)
        with self.assertRaises(subprocess.TimeoutExpired):
            second.communicate(timeout=0.2)
        self.assert_submission_count(0)
        (self.work / "bootstrap-release").touch()
        for process in (first, second):
            output = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, output)
        self.finish()
        self.check_completion()


class ExplicitStagingInitializationTests(StorageInitializationTests):
    staging = "explicit-staging"

    def configure_workflow(self, settings=""):
        settings = ", ".join(filter(None, (settings, "results_staging_root='explicit-staging'")))
        test_storage.StoragePlacementTests.configure_workflow(self, settings=settings)
