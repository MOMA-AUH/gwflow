"""Interrupted allocation recovery through the installed public commands."""

import json
import subprocess
import sys

from gwf.backends.local import Client, get_socket_path

from support import FIXTURES, LocalBackendTestCase
import test_fresh


class AttemptInitializationTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow

    def interrupt(self, phase, code, *, replacement=False):
        arguments = ["run", "--force", "a"] if replacement else ["run", "a"]
        with Client.connect(get_socket_path(self.work)) as client:
            before = client.status()
        result = subprocess.run(
            [sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "a", *arguments],
            cwd=self.work, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNoSubmissions(result.stdout + result.stderr)
        with Client.connect(get_socket_path(self.work)) as client:
            self.assertEqual(client.status(), before)
        return result

    def test_first_initial_record_publication_can_restart(self):
        self.check_first("before_attempt_record", 120)

    def check_first(self, phase, code, *, selected=False):
        self.interrupt(phase, code)
        self.cli("status", "--json")
        self.cli("recover", "a")
        pending = self.task_attempts()["a"] if selected else None
        self.cli("run", "b", "--dry-run")
        self.run_complete("b")
        self.cli("clean", "b", "--delete")
        self.assertFalse((self.work / "results/a").exists())
        self.run_complete("a")
        if selected:
            self.assertEqual(self.task_attempts()["a"], pending)
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["b", "a"])
        self.cli("clean", "--delete")

    def check_replacement(self, phase, code, *, selected=False, ready=False):
        self.run_complete("a")
        previous = self.task_attempts()["a"]
        retained = self.work / "results/a/result.txt"
        before = retained.stat()
        self.interrupt(phase, code, replacement=True)
        pending = self.task_attempts()["a"]
        if selected:
            self.assertNotEqual(pending, previous)
        else:
            self.assertEqual(pending, previous)
        if not ready:
            self.assertEqual(retained.read_text(), "a")
            after = retained.stat()
            # Reading the contents can update access time on this filesystem.
            self.assertEqual((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                             (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns))
        self.cli("status", "--json")
        self.cli("recover", "a")
        self.cli("clean", "--delete")
        self.cli("clean", "--delete", "--attempt", previous)
        self.assertEqual(self.task_attempts()["a"], pending)
        if not selected:
            self.assertNoSubmissions(self.cli("run", "a"))
            self.assertEqual(self.task_attempts()["a"], previous)
            self.assertEqual(retained.read_text(), "a")
        self.run_complete("a", *([] if selected else ["--force"]))
        self.assertEqual(retained.read_text(), "a")
        if selected:
            self.assertEqual(self.task_attempts()["a"], pending)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["a", "a"])
        self.cli("clean", "--delete")

    def test_first_private_allocation_can_restart(self):
        self.check_first("after_private_allocation", 128)

    def test_first_attempt_allocation_can_restart(self):
        self.check_first("after_attempt_allocation", 127)

    def test_first_unpublished_record_state_can_restart(self):
        self.check_first("before_record_state", 125)

    def test_first_published_record_state_can_restart(self):
        self.check_first("after_record_state", 126)

    def test_first_published_initial_record_can_restart(self):
        self.check_first("after_attempt_record", 122)

    def test_first_uninstalled_attempt_can_restart(self):
        self.check_first("before_attempt_install", 123)

    def test_first_installed_attempt_can_restart(self):
        self.check_first("after_attempt_install", 124)

    def test_first_unselected_attempt_can_restart(self):
        self.check_first("before_selection", 90)

    def test_first_selected_attempt_keeps_identity(self):
        self.check_first("before_removal", 92, selected=True)

    def test_first_unpublished_readiness_keeps_identity(self):
        self.check_first("before_ready", 121, selected=True)

    def test_first_ready_attempt_keeps_identity(self):
        self.check_first("after_ready", 93, selected=True)

    def test_replacement_private_allocation_preserves_selection(self):
        self.check_replacement("after_private_allocation", 128)

    def test_replacement_attempt_allocation_preserves_selection(self):
        self.check_replacement("after_attempt_allocation", 127)

    def test_replacement_unpublished_record_state_preserves_selection(self):
        self.check_replacement("before_record_state", 125)

    def test_replacement_published_record_state_preserves_selection(self):
        self.check_replacement("after_record_state", 126)

    def test_replacement_initial_record_publication_preserves_selection(self):
        self.check_replacement("before_attempt_record", 120)

    def test_replacement_published_initial_record_preserves_selection(self):
        self.check_replacement("after_attempt_record", 122)

    def test_replacement_uninstalled_attempt_preserves_selection(self):
        self.check_replacement("before_attempt_install", 123)

    def test_replacement_installed_attempt_preserves_selection(self):
        self.check_replacement("after_attempt_install", 124)

    def test_replacement_unselected_attempt_preserves_selection(self):
        self.check_replacement("before_selection", 90)

    def test_replacement_selected_attempt_keeps_identity(self):
        self.check_replacement("before_removal", 92, selected=True)

    def test_replacement_unpublished_readiness_keeps_identity(self):
        self.check_replacement("before_ready", 121, selected=True, ready=True)

    def test_replacement_ready_attempt_keeps_identity(self):
        self.check_replacement("after_ready", 93, selected=True, ready=True)

    def assert_first_attempt_refused(self):
        root = self.work / ".gwf/gwflow"
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        for arguments in (("run", "a"), ("run", "b", "--dry-run"), ("status", "--json"),
                          ("recover", "a"), ("clean", "--delete")):
            with self.subTest(arguments=arguments):
                self.assertNoSubmissions(self.cli(*arguments, success=False))
                self.assertEqual({path: path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)
        with Client.connect(get_socket_path(self.work)) as client:
            self.assertEqual(client.status(), {})

    def test_missing_published_initial_record_is_not_an_unfinished_allocation(self):
        self.interrupt("before_selection", 90)
        root = self.work / ".gwf/gwflow"
        attempt = next(root.glob("owners/*/tasks/a/attempts/*/attempt.json"))
        attempt.unlink()
        self.assert_first_attempt_refused()

    def test_unowned_task_directory_is_not_adopted_after_interruption(self):
        self.interrupt("after_private_allocation", 128)
        root = self.work / ".gwf/gwflow"
        owner = json.loads((root / "owner.json").read_text())["owner"]
        unowned = root / "owners" / owner / "tasks/a"
        unowned.mkdir(parents=True)
        (unowned / "unowned.txt").write_text("preserve")
        self.assert_first_attempt_refused()

    def test_unowned_historical_attempt_still_blocks_cleanup(self):
        self.run_complete("a")
        self.interrupt("before_attempt_record", 120, replacement=True)
        root = self.work / ".gwf/gwflow"
        attempts = next(root.glob("owners/*/tasks/a/attempts"))
        unowned = attempts / ("f" * 32)
        unowned.mkdir()
        marker = unowned / "unowned.txt"
        marker.write_text("preserve")
        self.assertNoSubmissions(self.cli("clean", "--delete", success=False))
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
