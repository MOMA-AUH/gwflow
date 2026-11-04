"""Local Cancellation owns the computation tree through the installed CLI."""

from collections import Counter
import os
from pathlib import Path
import re
import shlex
import signal

from support import GWF, LocalBackendTestCase
import test_graphs
import test_registry_images


class LocalCancellationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
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
        attempt = re.search(r"attempt ([0-9a-f]{32})", self.cli("logs", "sample", "--details")).group(1)
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

    def supervisor_loss_after_restart(self, **injection):
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
        pid = int((self.work / "pids").read_text().split()[0])
        while pid > 1:
            process = Path(f"/proc/{pid}")
            if b"gwflow._worker_process" in process.joinpath("cmdline").read_bytes().split(b"\0"):
                os.kill(pid, signal.SIGKILL)
                break
            pid = int(process.joinpath("stat").read_text().rsplit(")", 1)[1].split()[1])
        else:
            self.fail("No local supervisor owns the foreground command")
        foreground_release.touch()
        self.wait_for(lambda: "sample__right: complete" in self.cli(
            "-b", "recovery_fixture", "logs", "sample", "--details", env=environment))
        self.settle()
        index = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
        self.assert_unsafe_attempt_blocked(attempt)
        self.stop_worker(self.worker)
        self.worker = self.start_worker()
        self.assert_unsafe_attempt_blocked(attempt)
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

    def running_probe_processes(self):
        running = []
        for pid in map(int, (self.work / "pids").read_text().split()):
            path = Path(f"/proc/{pid}/stat")
            if path.exists() and path.read_text().rsplit(")", 1)[1].split()[0] != "Z":
                running.append(pid)
        return running

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

    def probe(self):
        release = self.work / "release"
        self.addCleanup(release.touch)
        pids = self.work / "pids"

        def cleanup():
            if pids.exists():
                for pid in map(int, pids.read_text().split()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.addCleanup(cleanup)
        child = (f"echo $BASHPID >> {shlex.quote(str(pids))}; "
                 f"touch {shlex.quote(str(self.work / 'held'))}; "
                 f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
                 f"touch {shlex.quote(str(self.work / 'late-write'))}")
        return (f"echo $BASHPID >> {shlex.quote(str(pids))}; "
                f"/bin/bash -c {shlex.quote(child)} & wait; printf right > same.txt")

    def assert_probe_stopped(self):
        self.assertEqual(self.running_probe_processes(), [],
                         "Computation processes still run after confirmed inactivity")

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
