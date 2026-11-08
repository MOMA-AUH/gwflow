"""Local Cancellation owns the computation tree through the installed CLI."""

from collections import Counter
import json
import os
import re
import shlex
import signal
import socket

from support import GWF, LocalBackendTestCase
import test_graphs
import test_registry_images
from process_probe import ProcessProbe


class LocalCancellationTests(ProcessProbe, LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = LocalBackendTestCase.settle
    inject = test_graphs.TaskGraphTests.inject

    def test_old_workers_allow_inspection_but_refuse_new_managed_jobs(self):
        self.stop_worker(self.worker)
        self.executable = GWF
        self.worker = self.start_worker()
        del self.executable
        self.assertIn("Run", self.cli("run", "--dry-run"))
        output = self.cli("run", success=False)
        self.assertIn("gwflow workers", output)
        self.assertFalse((self.work / "trace").exists())
        self.stop_worker(self.worker)
        self.worker = self.start_worker()
        self.run_complete()
        self.assertIn("Reuse", self.cli("run", "--dry-run"))

    def test_old_worker_cancellation_cannot_authorize_retry_replacement_or_cleanup(self):
        self.stop_worker(self.worker)
        self.executable = GWF
        self.worker = self.start_worker()
        del self.executable
        self.configure_workflow(right_command=self.probe())
        # Existing backend fixture submits through the original local protocol,
        # representing work admitted before the frontend was upgraded.
        self.cli("-b", "recovery_fixture", "run", env=self.inject())
        self.wait_for((self.work / "held").exists)
        attempt = re.search(r"attempt ([0-9a-f]{32})", self.cli(
            "-b", "recovery_fixture", "logs", "sample", "--details", env=self.inject())).group(1)
        self.assertIn("cannot verify cancellation identity", self.cli("cancel", "sample", success=False))
        self.cli("-b", "recovery_fixture", "cancel", "sample", env=self.inject())
        self.settle()
        self.assertIn("blocked", self.cli("status", "sample", "--plain"))
        for arguments in (("run", "sample"), ("run", "sample", "--force"),
                          ("clean", "--attempt", attempt, "--delete")):
            self.assertIn("cannot confirm computation descendants have stopped", self.cli(*arguments, success=False))
        # Restart loses scheduler history, but does not stop the old child.
        self.stop_worker(self.worker)
        self.worker = self.start_worker()
        for arguments in (("run", "sample"), ("run", "sample", "--force"),
                          ("clean", "--attempt", attempt, "--delete")):
            self.assertIn("cannot confirm computation descendants have stopped", self.cli(*arguments, success=False))

    def test_retry_keeps_completed_sibling_attempt_and_submission_history(self):
        trace = shlex.quote(str(self.work / "trace"))
        self.configure_workflow(right_command=f"echo right >> {trace}; " + self.probe())
        self.cli("run")
        self.wait_for((self.work / "held").exists)
        self.wait_for(lambda: "sample__left: complete" in self.cli("logs", "sample"))
        before = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", before).group(1)
        submission = re.search(r"sample__right: running; submission (\S+)", before).group(1)
        self.cli("cancel", "sample")
        self.cli("cancel", "sample")
        self.settle()
        self.assert_probe_stopped()
        (self.work / "release").touch()
        self.cli("run", "sample")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 1, "right": 2, "join": 1})
        self.assertIn(attempt, self.cli("logs", "sample", "--details"))
        self.assertIn(submission, self.cli("logs", "sample", "--history"))

    def test_forgotten_failed_job_with_child_cannot_authorize_retry_or_cleanup(self):
        attempt = self.failed_job_after_worker_restart()
        self.assert_unsafe_attempt_blocked(attempt)

    def test_supervisor_loss_stays_unresolved_after_checked_success_and_worker_restart(self):
        self.supervisor_loss_after_restart(interrupt_after_acceptance="sample__right")

    def test_supervisor_loss_without_backend_id_stays_unresolved_after_worker_restart(self):
        self.supervisor_loss_after_restart(lose_tracking="sample__right")

    def test_supervisor_loss_stays_unresolved_after_worker_port_changes(self):
        self.supervisor_loss_after_restart(change_port=True, interrupt_after_acceptance="sample__right")

    def supervisor_loss_after_restart(self, *, change_port=False, **injection):
        foreground_release = self.work / "foreground-release"
        self.addCleanup(foreground_release.touch)
        trace = shlex.quote(str(self.work / "trace"))
        command = self.probe().replace(
            " & wait; printf right > same.txt",
            " >/dev/null 2>&1 & "
            f"while [ ! -e {shlex.quote(str(foreground_release))} ]; do sleep 0.025; done; "
            "printf right > same.txt",
        )
        self.configure_workflow(right_command=f"echo right >> {trace}; " + command)
        environment = self.inject(**injection)
        self.cli("-b", "recovery_fixture", "run", env=environment, success=False)
        self.wait_for((self.work / "held").exists)
        # Follow the foreground command's actual ancestry, not a guessed PID.
        # Killing the supervisor models loss of its cleanup attestation.
        os.kill(self.supervisor_pid(), signal.SIGKILL)
        foreground_release.touch()
        self.wait_for(lambda: "sample__right: complete" in self.cli(
            "-b", "recovery_fixture", "logs", "sample", "--details", env=environment))
        self.settle()
        index = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
        self.assert_unsafe_attempt_blocked(attempt)
        self.restart_worker(change_port=change_port)
        self.assert_unsafe_attempt_blocked(attempt)
        if change_port:
            environment = self.state_backend({})
            self.assertIn("cannot confirm computation descendants have stopped", self.cli(
                "-b", "state_fixture", "run", "sample", "--force", env=environment, success=False))
            self.assertIn("cannot confirm computation descendants have stopped", self.cli(
                "-b", "state_fixture", "clean", "sample", "--delete", env=environment, success=False))
        self.assertIn("cannot confirm computation descendants have stopped",
                      self.cli("run", "sample", "--dry-run", success=False))
        self.assertIn("cannot confirm computation descendants have stopped",
                      self.cli("clean", "sample", "--delete", success=False))
        # The survivor can still write; recovery is only valid after it stops.
        (self.work / "release").touch()
        self.wait_for((self.work / "late-write").exists)
        self.wait_for(lambda: not self.running_probe_processes())
        self.cli("recover", "sample")
        self.run_complete("sample")
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()),
                         {"left": 1, "right": 1, "join": 1})

    def restart_worker(self, *, change_port=False, working_dir=None):
        self.stop_worker(self.worker)
        if change_port:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]
            self.configure(**{"backend.local.port": self.port})
        self.worker = self.start_worker(working_dir=working_dir)

    def test_completed_work_keeps_receipts_across_worker_locations_and_backends(self):
        self.restart_worker(working_dir=self.make_workdir())
        self.run_complete()
        self.restart_worker(change_port=True, working_dir=self.make_workdir())
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        environment = self.state_backend({})
        self.assertIn("Reuse", self.cli("-b", "state_fixture", "run", "sample", "--dry-run", env=environment))
        self.cli("-b", "state_fixture", "clean", "sample", "--delete", env=environment)
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()),
                         {"left": 1, "right": 1, "join": 1})

    def test_missing_malformed_or_foreign_receipt_never_proves_inactivity(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
        submission = re.search(r"sample__right: complete; submission (\S+)", index).group(1)
        receipt = next(path for path in (self.work / ".gwf/local-termination").glob("*.json")
                       if json.loads(path.read_text())["submission"] == submission)
        original = receipt.read_bytes()
        malformed = (None, b"{broken", json.dumps({"submission": "another-submission", "state": "COMPLETED"}).encode(),
                     json.dumps({"submission": submission, "state": "RUNNING"}).encode())
        for contents in malformed:
            with self.subTest(contents=contents):
                self.stop_worker(self.worker)
                if contents is None:
                    receipt.unlink()
                else:
                    receipt.write_bytes(contents)
                self.worker = self.start_worker()
                self.assert_unsafe_attempt_blocked(attempt)
                receipt.write_bytes(original)
                self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))

    def test_confirmed_completion_remains_reusable_after_worker_restart(self):
        self.run_complete()
        self.stop_worker(self.worker)
        self.worker = self.start_worker()
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        self.cli("clean", "sample", "--delete")
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()),
                         {"left": 1, "right": 1, "join": 1})

    def test_confirmed_cancellation_preserves_partial_work_after_worker_restart(self):
        trace = shlex.quote(str(self.work / "trace"))
        self.configure_workflow(right_command=f"echo right >> {trace}; " + self.probe())
        self.cli("run")
        self.wait_for((self.work / "held").exists)
        self.wait_for(lambda: "sample__left: complete" in self.cli("logs", "sample"))
        before = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", before).group(1)
        self.cli("cancel", "sample")
        self.settle()
        self.assert_probe_stopped()
        self.stop_worker(self.worker)
        self.worker = self.start_worker()
        self.assertIn("Retry", self.cli("run", "sample", "--dry-run"))
        (self.work / "release").touch()
        self.run_complete("sample")
        self.assertIn(attempt, self.cli("logs", "sample", "--details"))
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()),
                         {"left": 1, "right": 2, "join": 1})

    def test_reused_job_id_never_cancels_other_task_or_authorizes_old_retry(self):
        attempt = self.failed_job_after_worker_restart()
        release = self.work / "other-release"
        self.addCleanup(release.touch)
        command = (f"touch {shlex.quote(str(self.work / 'other-held'))}; "
                   f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf other > out")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                            "other = empty_task(inputs=[])\n"
                            "first = other.target('first', inputs=[], outputs=['first'])\n"
                            "first << 'touch first'\n"
                            "hold = other.target('hold', inputs=[first.output('first')], outputs=['out'])\n"
                            f"hold << {command!r}\n"
                            "other.retain('result', source=hold.output('out'), path='out')\n"
                            "gwf.task(other, alias='other')\n")
        self.cli("run", "other")
        self.wait_for((self.work / "other-held").exists)
        self.assertIn("different submission", self.cli("cancel", "sample", success=False))
        self.assertIn("running", self.cli("status", "other", "--plain"))
        self.assert_unsafe_attempt_blocked(attempt)
        release.touch()
        self.finish()
        self.assertEqual((self.work / "results/other/out").read_text(), "other")
        self.assert_unsafe_attempt_blocked(attempt)

    def assert_unsafe_attempt_blocked(self, attempt):
        self.assertIn("blocked", self.cli("status", "sample", "--plain"))
        for arguments in (("run", "sample"), ("run", "sample", "--force"),
                          ("clean", "--attempt", attempt, "--delete")):
            self.assertIn("cannot confirm computation descendants have stopped", self.cli(*arguments, success=False))

    def failed_job_after_worker_restart(self):
        self.stop_worker(self.worker)
        self.executable = GWF
        self.worker = self.start_worker()
        del self.executable
        # A failing foreground shell can leave ordinary background children.
        # Close their log pipes so the old worker observes the recorded failure.
        command = self.probe().replace(" & wait; printf right > same.txt", " >/dev/null 2>&1 & exit 8")
        self.configure_workflow(right_command=command)
        environment = self.inject(interrupt_after_acceptance="sample__right")
        self.cli("-b", "recovery_fixture", "run", env=environment, success=False)
        self.wait_for((self.work / "held").exists)
        self.settle()
        index = self.cli("-b", "recovery_fixture", "logs", "sample", "--details", env=environment)
        self.assertIn("sample__right: failed", index)
        self.assertIn("sample__gwflow_complete: pending; submission not submitted", index)
        attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
        self.stop_worker(self.worker)
        self.worker = self.start_worker()
        return attempt

    def test_cancel_does_not_stop_unselected_running_task(self):
        self.configure_workflow(right_command=self.probe())
        release = self.work / "other-release"
        self.addCleanup(release.touch)
        command = (f"touch {shlex.quote(str(self.work / 'other-held'))}; "
                   f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf other > out")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                            "other = empty_task(inputs=[])\n"
                            "target = other.target('compute', inputs=[], outputs=['out'])\n"
                            f"target << {command!r}\n"
                            "other.retain('result', source=target.output('out'), path='out')\n"
                            "gwf.task(other, alias='other')\n")
        self.cli("run")
        self.wait_for((self.work / "held").exists)
        self.wait_for((self.work / "other-held").exists)
        self.cli("cancel", "sample")
        self.wait_for(lambda: "Retry" in self.cli("run", "sample", "--dry-run"))
        self.assert_probe_stopped()
        self.assertIn("running", self.cli("status", "other", "--plain"))
        release.touch()
        self.settle()
        self.assertEqual((self.work / "results/other/out").read_text(), "other")

    def test_stopping_workers_stops_owned_computation_children(self):
        self.configure_workflow(right_command=self.probe())
        self.cli("run")
        self.wait_for((self.work / "held").exists)
        self.stop_worker(self.worker)
        self.assert_probe_stopped()

    def test_cancel_stops_bash_and_child_before_explicit_cleanup(self):
        self.configure_workflow(right_command=self.probe())
        self.cli("run")
        self.wait_for((self.work / "held").exists)
        attempt = re.search(r"attempt ([0-9a-f]{32})", self.cli("logs", "sample", "--details")).group(1)
        output = self.cli("cancel", "sample")
        self.assertIn("may still be stopping", output)
        self.settle()
        self.assertIn("Retry", self.cli("run", "sample", "--dry-run"))
        self.assert_probe_stopped()
        self.cli("clean", "--attempt", attempt, "--delete")
        (self.work / "release").touch()
        self.assertFalse((self.work / "late-write").exists())


class ContainerCancellationTests(test_registry_images.RegistryTestCase):
    probe = LocalCancellationTests.probe
    running_probe_processes = LocalCancellationTests.running_probe_processes
    assert_probe_stopped = LocalCancellationTests.assert_probe_stopped
    test_cancel_stops_container_command_and_child_before_cleanup = LocalCancellationTests.test_cancel_stops_bash_and_child_before_explicit_cleanup

    def configure_workflow(self, **options):
        test_registry_images.RegistryTestCase.configure_workflow(self)
        test_graphs.TaskGraphTests.configure_workflow(self, **options)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "right = task.target('right', inputs=[], outputs=['same.txt'])",
            f"right = task.target('right', inputs=[], outputs=['same.txt'], image={self.reference!r})"))
