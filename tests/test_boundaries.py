"""Task boundary checks through the installed gwf command."""

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


GWF = str(Path(sys.executable).with_name("gwf"))


class BoundaryCliTests(unittest.TestCase):
    def start_worker(self, work):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        (work / ".gwfconf.json").write_text(
            json.dumps({"backend": "local", "backend.local.port": port})
        )
        worker = subprocess.Popen(
            [GWF, "workers", "-n", "2", "-p", str(port)],
            cwd=work,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                self.fail(f"gwf workers exited: {worker.stderr.read()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return worker
            except OSError:
                time.sleep(0.1)
        worker.terminate()
        worker.communicate(timeout=10)
        self.fail("gwf workers did not start")

    def stop_worker(self, worker):
        worker.terminate()
        try:
            worker.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate(timeout=10)

    def run_workflow(self, source, files=()):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "workflow.py").write_text(source)
            for path in files:
                (work / path).write_text("already here\n")
            worker = self.start_worker(work)
            try:
                result = subprocess.run(
                    [GWF, "run"], cwd=work, capture_output=True, text=True, timeout=30
                )
            finally:
                self.stop_worker(worker)
            self.assertFalse((work / "ran.txt").exists(), result.stdout + result.stderr)
            return result

    def test_existing_undeclared_inner_input_is_rejected(self):
        result = self.run_workflow(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[], outputs=['out.txt'])\n"
            "task.target('use', inputs=['hidden.txt'], outputs=['out.txt']) << "
            "'touch ran.txt; cp hidden.txt out.txt'\n"
            "gwf.task_from_template('consumer', task)\n",
            files=("hidden.txt",),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external input", result.stdout + result.stderr)
        self.assertIn("hidden.txt", result.stdout + result.stderr)

    def test_unproduced_retained_output_is_rejected(self):
        result = self.run_workflow(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[], outputs=['claimed.txt'])\n"
            "task.target('make', inputs=[], outputs=['actual.txt']) << "
            "'touch ran.txt actual.txt'\n"
            "gwf.task_from_template('producer', task)\n",
            files=("claimed.txt",),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retained output", result.stdout + result.stderr)
        self.assertIn("claimed.txt", result.stdout + result.stderr)

    def test_outputless_inner_target_is_rejected(self):
        result = self.run_workflow(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[], outputs=[])\n"
            "task.target('side_effect', inputs=[], outputs=[]) << 'touch ran.txt'\n"
            "gwf.task_from_template('producer', task)\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outputless", result.stdout + result.stderr)
        self.assertIn("side_effect", result.stdout + result.stderr)

    def test_existing_internal_intermediate_cannot_cross_task_boundary(self):
        result = self.run_workflow(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "consumer = Task(inputs=['middle.txt'], outputs=['used.txt'])\n"
            "consumer.target('use', inputs=['middle.txt'], outputs=['used.txt']) << "
            "'touch ran.txt; cp middle.txt used.txt'\n"
            "gwf.task_from_template('consumer', consumer)\n"
            "producer = Task(inputs=[], outputs=['final.txt'])\n"
            "producer.target('middle', inputs=[], outputs=['middle.txt']) << "
            "'echo new > middle.txt'\n"
            "producer.target('final', inputs=['middle.txt'], outputs=['final.txt']) << "
            "'cp middle.txt final.txt'\n"
            "gwf.task_from_template('producer', producer)\n",
            files=("middle.txt",),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retained output", result.stdout + result.stderr)
        self.assertIn("middle.txt", result.stdout + result.stderr)

    def test_retained_output_connects_tasks_through_gwf_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "seed.txt").write_text("hello\n")
            (work / "hidden.txt").write_text("present\n")
            (work / "workflow.py").write_text(
                "from gwflow import Task, Workflow\n"
                "gwf = Workflow()\n"
                "consumer = Task(inputs=['produced.txt'], outputs=['result.txt'])\n"
                "consumer.target('use', inputs=['produced.txt'], outputs=['result.txt']) << "
                "'cat produced.txt > result.txt'\n"
                "gwf.task_from_template('consumer', consumer)\n"
                "producer = Task(inputs=['seed.txt'], outputs=['produced.txt'])\n"
                "producer.target('make', inputs=['seed.txt'], outputs=['produced.txt']) << "
                "\"test -f hidden.txt && tr '[:lower:]' '[:upper:]' "
                "< seed.txt > produced.txt\"\n"
                "gwf.task_from_template('producer', producer)\n"
            )
            worker = self.start_worker(work)
            try:
                run = subprocess.run(
                    [GWF, "run"], cwd=work, capture_output=True, text=True, timeout=30
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                result = work / "result.txt"
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if result.exists() and result.read_text() == "HELLO\n":
                        break
                    time.sleep(0.1)
                self.assertEqual(result.read_text(), "HELLO\n")
            finally:
                self.stop_worker(worker)

    def test_duplicate_producers_still_fail(self):
        result = self.run_workflow(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "for name in ('first', 'second'):\n"
            "    task = Task(inputs=[], outputs=['same.txt'])\n"
            "    task.target('make', inputs=[], outputs=['same.txt']) << "
            "'touch ran.txt same.txt'\n"
            "    gwf.task_from_template(name, task)\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provided by targets", result.stdout + result.stderr)

    def test_cycle_still_fails(self):
        result = self.run_workflow(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[], outputs=['a.txt', 'b.txt'])\n"
            "task.target('a', inputs=['b.txt'], outputs=['a.txt']) << "
            "'touch ran.txt a.txt'\n"
            "task.target('b', inputs=['a.txt'], outputs=['b.txt']) << "
            "'touch ran.txt b.txt'\n"
            "gwf.task_from_template('cycle', task)\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depends on itself", result.stdout + result.stderr)

    def test_missing_external_file_still_fails(self):
        result = self.run_workflow(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=['missing.txt'], outputs=['out.txt'])\n"
            "task.target('use', inputs=['missing.txt'], outputs=['out.txt']) << "
            "'touch ran.txt out.txt'\n"
            "gwf.task_from_template('consumer', task)\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
