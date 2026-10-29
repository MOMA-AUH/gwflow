"""Executable ownership and discovery through the installed commands."""

import importlib.metadata
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


GWFLOW = str(Path(sys.executable).with_name("gwflow"))


class StartupTests(unittest.TestCase):
    def test_unknown_backend_names_remain_usage_errors(self):
        with tempfile.TemporaryDirectory(prefix="gwflow backend choice ") as directory:
            for command in ('wait', 'status', 'run'):
                with self.subTest(command=command):
                    result = subprocess.run([GWFLOW, '--backend', 'unknown_backend', command], cwd=directory,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('--backend', result.stderr)
                    self.assertIn('local', result.stderr)
                    self.assertEqual(result.stdout, '')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_help_version_and_missing_workflow_without_state(self):
        with tempfile.TemporaryDirectory(prefix="gwflow startup ") as directory:
            work = Path(directory)
            for arguments in (("--help",), ("--version",),
                              *((command, "--help") for command in
                                ("run", "status", "logs", "cancel", "recover", "clean", "config", "workers", "wait"))):
                result = subprocess.run([GWFLOW, *arguments], cwd=work, input="",
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if arguments == ("--help",):
                    commands = {line.split()[0] for line in result.stdout.split("Commands:")[1].splitlines() if line.strip()}
                    self.assertEqual(commands, {"run", "status", "logs", "cancel", "recover", "clean", "config", "workers", "wait"})
                if arguments == ("--version",):
                    self.assertIn("gwflow", result.stdout)
                    self.assertIn(importlib.metadata.version("gwflow"), result.stdout)
            for command in ("run", "status", "clean", "logs", "cancel", "recover", "wait"):
                arguments = [command, "sample"] if command in ("logs", "cancel", "recover") else [command]
                result = subprocess.run([GWFLOW, *arguments], cwd=work, input="",
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--file", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(list(work.iterdir()), [])

    def test_ordinary_commands_reject_managed_workflows_before_execution(self):
        with tempfile.TemporaryDirectory(prefix="gwflow boundary ") as directory:
            work = Path(directory)
            (work / "workflow.py").write_text("from gwflow import Workflow\ngwf = Workflow()\n")
            for command in (("run",), ("status",), ("info",), ("touch",),
                            ("clean", "--force"), ("cancel", "--force")):
                result = subprocess.run([str(Path(sys.executable).with_name("gwf")), "-b", "local", *command],
                                        cwd=work, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("use gwflow", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((work / "work").exists())
            self.assertFalse((work / "results").exists())
            self.assertEqual(list((work / ".gwf").rglob("*.json")), [])

    def test_discovery_and_wrong_workflow_type(self):
        with tempfile.TemporaryDirectory(prefix="gwflow discovery ") as directory:
            work = Path(directory)
            nested = work / "nested"
            nested.mkdir()
            (work / "workflow.py").write_text(
                "from gwflow import Workflow\nfrom pathlib import Path\n"
                "with Path(__file__).with_name('loads.txt').open('a') as stream: stream.write('load\\n')\n"
                "gwf = Workflow()\ncustom = gwf\n")
            shutil.copy(Path(__file__).parent / "fixtures/state_backend.py", work)
            metadata = work / "state_backend-1.0.dist-info"
            metadata.mkdir()
            (metadata / "entry_points.txt").write_text("[gwf.backends]\nstate_fixture = state_backend:setup\n")
            (work / "backend-state.json").write_text("{}")
            environment = {**os.environ, "PYTHONPATH": str(work)}
            for file_options in ((), ("--file", "workflow.py"), ("--file", "workflow.py:custom")):
                result = subprocess.run([GWFLOW, "-b", "state_fixture", *file_options, "status"], cwd=nested,
                                        capture_output=True, text=True, timeout=30, env=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((work / "loads.txt").read_text(), "load\n" * 3)
            self.assertFalse((nested / ".gwf").exists())
            (work / "ordinary.py").write_text("from gwf import Workflow\ngwf = Workflow()\n")
            for command in (("run",), ("status",), ("wait",), ("clean", "--delete"), ("logs", "anything"), ("cancel", "anything"), ("recover", "anything")):
                result = subprocess.run([GWFLOW, "--file", "ordinary.py", *command], cwd=nested,
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("use gwf", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            for command in ("explain", "info", "touch", "clean-work"):
                result = subprocess.run([GWFLOW, command], cwd=nested, capture_output=True,
                                        text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("No such command", result.stderr)
            self.assertFalse((work / "work").exists())
            self.assertFalse((work / "results").exists())

    def test_configuration_without_workflow(self):
        with tempfile.TemporaryDirectory(prefix="gwflow config ") as directory:
            for arguments, expected in ((("set", "backend", "local"), ""),
                                        (("get", "backend"), "local"),
                                        (("unset", "backend"), ""),
                                        (("get", "backend"), "<not set>")):
                result = subprocess.run([GWFLOW, "config", *arguments], cwd=directory,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
            self.assertFalse((Path(directory) / "workflow.py").exists())

    def test_task_job_collisions_fail_loading_before_command_effects(self):
        with tempfile.TemporaryDirectory(prefix="gwflow names ") as directory:
            work = Path(directory)
            declaration = (
                "from gwflow import Task, Workflow, task_template\n"
                "@task_template\n"
                "def producer():\n"
                "    task = Task(inputs=[])\n"
                "    task.target('copy', inputs=[], outputs=['out']) << 'touch out'\n"
                "    return task\n"
                "@task_template\n"
                "def other():\n"
                "    return Task(inputs=[])\n"
                "gwf = Workflow()\n"
            )
            for local in ("copy", "gwflow_prepare", "gwflow_complete"):
                name = f"sample__{local}"
                registrations = ["gwf.task(producer(), alias='sample')\n",
                                 f"gwf.task(other(), alias={name!r})\n"]
                for reverse in (False, True):
                    (work / "workflow.py").write_text(
                        declaration + "".join(reversed(registrations) if reverse else registrations))
                    for command in (("run",), ("status",), ("wait",), ("logs", name), ("cancel", name), ("recover", name), ("clean", "--delete")):
                        with self.subTest(local=local, reverse=reverse, command=command):
                            result = subprocess.run([GWFLOW, *command], cwd=work, input="",
                                                    capture_output=True, text=True, timeout=30)
                            self.assertNotEqual(result.returncode, 0)
                            for context in (name, "sample", "producer", "other", "collides"):
                                self.assertIn(context, result.stderr)
                            self.assertNotIn("Traceback", result.stderr)
                            self.assertFalse((work / "work").exists())
                            self.assertFalse((work / "results").exists())
                            self.assertEqual(list((work / ".gwf").rglob("*.json")), [])
