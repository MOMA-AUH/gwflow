"""Requested resource budgets and termination through installed commands."""

import json
import shlex
import time

from support import LocalBackendTestCase, TASK_FACTORY
from process_probe import ProcessProbe


class LocalResourceTests(ProcessProbe, LocalBackendTestCase):
    worker_memory = "2g"

    def configure_workflow(self, *, cores=2, memory="1g", command=None):
        first = command or self.gated("first")
        second = self.gated("second")
        (self.work / "workflow.py").write_text(
            TASK_FACTORY + "from gwflow import Workflow\ngwf = Workflow()\n"
            "for name, command in " + repr((("first", first), ("second", second))) + ":\n"
            "    task = empty_task(inputs=[])\n"
            f"    target = task.target('compute', inputs=[], outputs=['out'], cores={cores!r}, memory={memory!r})\n"
            "    target << command\n"
            "    task.retain('result', source=target.output('out'), path='out')\n"
            "    gwf.task(task, alias=name)\n")

    def gated(self, name):
        release = self.work / (name + "-release")
        self.addCleanup(release.touch)
        return (f"touch {shlex.quote(str(self.work / (name + '-held')))}; "
                f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf done > out")

    def queue_second(self):
        self.cli("run", "first")
        self.wait_for((self.work / "first-held").exists)
        self.cli("run", "second")
        self.assertIn("queued", self.cli("status", "second", "--plain"))
        self.assertFalse((self.work / "second-held").exists())

    def finish_second(self):
        self.wait_for((self.work / "second-held").exists)
        (self.work / "second-release").touch()
        self.wait_for((self.work / "results/second/out").exists)
        self.assertEqual((self.work / "results/second/out").read_text(), "done")

    def assert_second_remains_queued(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.assertIn("queued", self.cli("status", "second", "--plain"))
            self.assertFalse((self.work / "second-held").exists())

    def test_supervisor_loss_keeps_reservation_while_descendants_survive(self):
        import os
        import signal
        self.configure_workflow(command=self.probe(escape=True) + "; printf done > out")
        self.cli("run", "first")
        self.wait_for((self.work / "held").exists)
        self.cli("run", "second")
        os.kill(self.supervisor_pid(), signal.SIGKILL)
        self.wait_for(lambda: "blocked" in self.cli("status", "first", "--plain"))
        self.assertTrue(self.running_probe_processes())
        self.assert_second_remains_queued()
        self.assertIn("cannot confirm computation descendants have stopped",
                      self.cli("run", "first", "--dry-run", success=False))

    def test_cores_are_reserved_by_request_and_released_after_success(self):
        self.queue_second()
        (self.work / "first-release").touch()
        self.finish_second()
        self.finish()

    def test_memory_reservation_blocks_even_when_a_core_is_available(self):
        self.configure_workflow(cores=1, memory="2g")
        self.queue_second()
        (self.work / "first-release").touch()
        self.finish_second()
        self.finish()

    def test_failed_receipt_keeps_reservation_and_blocks_unsafe_operations(self):
        self.queue_second()
        receipt = next(path for path in (self.work / ".gwf/local-termination").glob("*.json")
                       if json.loads(path.read_text())["submission"].startswith("first__compute__"))
        receipt.unlink()
        receipt.mkdir()
        (self.work / "first-release").touch()
        self.wait_for(lambda: "cannot confirm computation descendants have stopped" in
                      self.cli("run", "first", "--dry-run", success=False))
        self.assert_second_remains_queued()
        for arguments in (("run", "first", "--force"), ("clean", "first", "--delete")):
            self.assertIn("cannot confirm computation descendants have stopped", self.cli(*arguments, success=False))

    def test_oversized_core_request_is_rejected_clearly(self):
        self.configure_workflow(cores=3)
        self.assertIn("requested 3 cores", self.cli("run", "first", success=False))
        self.assertFalse((self.work / "first-held").exists())

    def test_oversized_memory_request_is_rejected_clearly(self):
        self.configure_workflow(memory="3g")
        self.assertIn("requested 3.0GiB of memory", self.cli("run", "first", success=False))
        self.assertFalse((self.work / "first-held").exists())

    def test_failure_releases_reserved_capacity(self):
        self.configure_workflow(command=self.gated("first") + "; exit 8")
        self.queue_second()
        (self.work / "first-release").touch()
        self.finish_second()
        self.assertIn("Retry", self.cli("run", "first", "--dry-run"))
        self.assertFalse((self.work / "results/first/out").exists())

    def test_repeated_cancellation_and_queued_jobs_preserve_capacity(self):
        self.queue_second()
        self.assertIn("may still be stopping", self.cli("cancel", "second"))
        self.cli("cancel", "second")
        self.assertFalse((self.work / "second-held").exists())
        self.cli("cancel", "first")
        self.cli("cancel", "first")
        self.wait_for(lambda: "Retry" in self.cli("run", "first", "--dry-run"))
        self.cli("run", "second")
        self.finish_second()
        self.cli("cancel", "second")
        self.assertIn("Reuse", self.cli("run", "second", "--dry-run"))

    def test_resource_changes_preserve_reuse(self):
        (self.work / "first-release").touch()
        self.run_complete("first")
        self.configure_workflow(cores=1, memory="2g")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("cores=1,", "walltime='00:00:20', cores=1,"))
        self.assertIn("Reuse", self.cli("run", "first", "--dry-run"))
        self.assertNoSubmissions(self.cli("run", "first"))
        self.assertEqual((self.work / "results/first/out").read_text(), "done")

    def test_independent_workflow_budget_can_run_while_first_is_full(self):
        import subprocess
        from support import GWFLOW
        other = self.make_workdir()
        (other / "workflow.py").write_text((self.work / "workflow.py").read_text().replace(
            str(self.work), str(other)))
        (other / ".gwfconf.json").write_text(json.dumps(self.config))
        self.addCleanup((other / "first-release").touch)
        self.start_worker(working_dir=other)
        self.cli("run", "first")
        self.wait_for((self.work / "first-held").exists)
        result = subprocess.run([GWFLOW, "run", "first"], cwd=other, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for((other / "first-held").exists)
        self.assertFalse((self.work / "first-release").exists())
        (other / "first-release").touch()
        (self.work / "first-release").touch()
        self.finish()


class WalltimeTests(LocalBackendTestCase):
    worker_debug = True

    def configure_workflow(self, *, workflow=None, task=None, target=None, preparation=None, completion=None):
        (self.work / "workflow.py").write_text(
            TASK_FACTORY + "from gwflow import Workflow\n"
            f"gwf = Workflow(defaults={workflow!r}, preparation_defaults={preparation!r}, completion_defaults={completion!r})\n"
            f"task = empty_task(inputs=[], defaults={task!r})\n"
            f"target = task.target('compute', inputs=[], outputs=['out'], **{target or {}!r})\n"
            "target << 'printf done > out'\n"
            "task.retain('result', source=target.output('out'), path='out')\n"
            "gwf.task(task, alias='sample')\n")

    def assert_limits(self, expected):
        self.run_complete()
        self.worker.terminate()
        _, diagnostic = self.worker.communicate(timeout=10)
        self.assertEqual(self.worker.returncode, 0, diagnostic)
        for name, seconds in expected.items():
            line = next((line for line in diagnostic.splitlines()
                         if f"sample__{name}__" in line and "queued with" in line), "")
            self.assertRegex(line, rf"time limit {seconds}(?:\.0)?[, ]", diagnostic)

    def test_unspecified_preparation_computation_completion_use_one_hour(self):
        self.assert_limits({"gwflow_prepare": 3600, "compute": 3600, "gwflow_complete": 3600})

    def test_workflow_walltime_applies_to_all_jobs(self):
        self.configure_workflow(workflow={"walltime": "00:00:20"})
        self.assert_limits({"gwflow_prepare": 20, "compute": 20, "gwflow_complete": 20})

    def test_task_walltime_overrides_workflow_for_computation(self):
        self.configure_workflow(workflow={"walltime": "00:00:20"}, task={"walltime": "00:00:15"})
        self.assert_limits({"gwflow_prepare": 20, "compute": 15, "gwflow_complete": 20})

    def test_target_and_lifecycle_overrides_take_precedence(self):
        self.configure_workflow(workflow={"walltime": "00:00:20"}, task={"walltime": "00:00:15"},
                                target={"walltime": "00:00:12"}, preparation={"walltime": "00:00:10"},
                                completion={"walltime": "00:00:11"})
        self.assert_limits({"gwflow_prepare": 10, "compute": 12, "gwflow_complete": 11})

    def test_null_walltime_restores_upstream_default(self):
        self.configure_workflow(workflow={"walltime": None}, task={"walltime": None},
                                target={"walltime": None}, preparation={"walltime": None},
                                completion={"walltime": None})
        self.assert_limits({"gwflow_prepare": 3600, "compute": 3600, "gwflow_complete": 3600})
