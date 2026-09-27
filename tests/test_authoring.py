import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from gwf.exceptions import WorkflowError

from gwflow import Task, Workflow


class RegistrationTests(unittest.TestCase):
    def test_snapshot_names_paths_options_and_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = Workflow(working_dir=directory, defaults={"cores": 2})
            task = Task(inputs=["input.txt"], outputs=["result.txt"])
            original = task.target(
                "write", inputs=["input.txt"], outputs=["result.txt"], memory="4g"
            ) << "cat input.txt > result.txt"
            workflow.task_from_template("first", task)
            registered = workflow.targets["first__write"]
            original.spec = "false"
            original.outputs.append("changed.txt")
            task.outputs.append("changed.txt")
            task.target("later", inputs=[], outputs=["later.txt"]) << "touch later.txt"

            self.assertEqual(registered.spec, "cat input.txt > result.txt")
            self.assertEqual(registered.outputs, ["result.txt"])
            self.assertEqual(registered.working_dir, directory)
            self.assertEqual(registered.options["cores"], 2)
            self.assertEqual(registered.options["memory"], "4g")
            self.assertEqual(set(workflow.targets), {"first__write"})

            second = Task(inputs=[], outputs=["other.txt"])
            second.target("write", inputs=[], outputs=["other.txt"]) << "touch other.txt"
            workflow.task_from_template("second", second)
            self.assertEqual(set(workflow.targets), {"first__write", "second__write"})

    def test_registration_rejects_collisions_without_partial_changes(self):
        workflow = Workflow(working_dir=".")
        task = Task(inputs=[], outputs=["result.txt"])
        task.target("write", inputs=[], outputs=["result.txt"]) << "touch result.txt"
        workflow.task_from_template("first", task)

        with self.assertRaisesRegex(WorkflowError, "Task name"):
            workflow.task_from_template("first", task)
        with self.assertRaisesRegex(WorkflowError, "reserved"):
            workflow.target("first__gwflow_complete", inputs=[], outputs=[])
        workflow.target("other__gwflow_complete", inputs=[], outputs=[])
        with self.assertRaisesRegex(WorkflowError, "collision"):
            workflow.task_from_template("other", Task([], []))
        self.assertEqual(set(workflow.targets), {"first__write", "other__gwflow_complete"})

        reserved = Task([], [])
        reserved.target("gwflow_complete", inputs=[], outputs=["reserved.txt"]) << "touch reserved.txt"
        with self.assertRaisesRegex(WorkflowError, "reserved"):
            workflow.task_from_template("reserved", reserved)

    def test_duplicate_local_names_fail_clearly(self):
        task = Task([], [])
        task.target("write", inputs=[], outputs=["one.txt"]) << "touch one.txt"
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            task.target("write", inputs=[], outputs=["two.txt"])

    def test_qualified_name_collision_and_invalid_names(self):
        workflow = Workflow(working_dir=".")
        first = Task([], [])
        first.target("b__c", inputs=[], outputs=["a.txt"]) << "touch a.txt"
        workflow.task_from_template("a", first)
        second = Task([], [])
        second.target("c", inputs=[], outputs=["b.txt"]) << "touch b.txt"
        with self.assertRaisesRegex(WorkflowError, "collision"):
            workflow.task_from_template("a__b", second)
        self.assertEqual(set(workflow.targets), {"a__b__c"})
        with self.assertRaisesRegex(WorkflowError, "Invalid task name"):
            workflow.task_from_template("bad-name", second)

    def test_explicit_task_working_directory_is_preserved(self):
        task = Task([], [], working_dir="/tmp/task-work")
        task.target("write", inputs=[], outputs=["out.txt"]) << "touch out.txt"
        workflow = Workflow(working_dir="/tmp/pipeline-work")
        workflow.task_from_template("named", task)
        self.assertEqual(workflow.targets["named__write"].working_dir, "/tmp/task-work")

    def test_qualified_names_are_independent_of_registration_order(self):
        task = Task([], [])
        task.target("write", inputs=[], outputs=["out.txt"]) << "touch out.txt"
        first = Workflow(working_dir=".")
        second = Workflow(working_dir=".")
        for name in ("alpha", "beta"):
            first.task_from_template(name, task)
        for name in ("beta", "alpha"):
            second.task_from_template(name, task)
        self.assertEqual(set(first.targets), set(second.targets))


class LocalCliTests(unittest.TestCase):
    def test_installed_api_runs_imported_factory_through_gwf_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            shutil.copy(Path(__file__).parent / "fixtures" / "task_library.py", work)
            (work / "input.txt").write_text("hello\n")
            (work / "workflow.py").write_text(
                "from gwflow import Workflow\n"
                "from task_library import text_task\n"
                "gwf = Workflow()\n"
                "gwf.task_from_template('alpha', text_task('alpha'))\n"
                "gwf.task_from_template('beta', text_task('beta'))\n"
            )
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            (work / ".gwfconf.json").write_text(
                json.dumps({"backend": "local", "backend.local.port": port})
            )
            gwf = str(Path(sys.executable).with_name("gwf"))
            worker = subprocess.Popen(
                [gwf, "workers", "-n", "2", "-p", str(port)],
                cwd=work,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        self.fail(f"gwf workers exited: {worker.stderr.read()}")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("gwf workers did not start")

                run = subprocess.run(
                    [gwf, "run"], cwd=work, capture_output=True, text=True, timeout=30
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if all((work / f"{name}.out").exists() for name in ("alpha", "beta")):
                        break
                    time.sleep(0.1)
                self.assertEqual((work / "alpha.out").read_text(), "HELLO\n")
                self.assertEqual((work / "beta.out").read_text(), "HELLO\n")
                self.assertEqual((work / "alpha.tmp").read_text(), "hello\n")
                self.assertEqual((work / "beta.tmp").read_text(), "hello\n")
            finally:
                worker.terminate()
                try:
                    worker.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
