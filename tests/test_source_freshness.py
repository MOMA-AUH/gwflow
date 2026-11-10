"""Current workflow definitions through installed managed commands."""

import os
import json
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest

from support import LocalBackendTestCase
from terminal_session import TerminalSession


def replace_with_cached_source(test, path, old, new):
    """Leave timestamp-valid old bytecode beside a same-size source edit."""
    source = path.read_text()
    test.assertIn(old, source)
    changed = source.replace(old, new)
    test.assertEqual(len(source.encode()), len(changed.encode()))
    before = path.stat()
    cache = Path(py_compile.compile(str(path), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP))
    bytecode = cache.read_bytes()
    path.write_text(changed)
    # Reproduce a rapid edit, without advancing the whole-second cache key.
    timestamp = before.st_mtime_ns // 1_000_000_000 * 1_000_000_000
    timestamp += (before.st_mtime_ns % 1_000_000_000 + 1) % 1_000_000_000
    os.utime(path, ns=(before.st_atime_ns, timestamp))
    test.assertEqual(path.stat().st_size, before.st_size)
    test.assertEqual(path.stat().st_mtime_ns // 1_000_000_000,
                     before.st_mtime_ns // 1_000_000_000)
    test.assertNotEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
    test.assertEqual(cache.read_bytes(), bytecode)
    return cache, bytecode


class LoadingIsolationTests(unittest.TestCase):
    def test_embedded_cli_restores_python_settings_and_cleans_loading_files(self):
        with tempfile.TemporaryDirectory(prefix="gwflow loading isolation ") as directory:
            work = Path(directory)
            for failure in (False, True):
                with self.subTest(failure=failure):
                    (work / "workflow.py").write_text(
                        "import sys\nfrom pathlib import Path\n"
                        "Path('loading-cache').write_text(sys.pycache_prefix)\n"
                        "from gwflow import Workflow\ngwf = Workflow()\n" +
                        ("raise ValueError('authored failure')\n" if failure else ""))
                    script = (
                        "import click, sys\nfrom pathlib import Path\n"
                        "from gwflow.cli import main\n"
                        "sys.pycache_prefix = str(Path('caller-cache').absolute())\n"
                        "sys.dont_write_bytecode = False\n"
                        "previous = sys.pycache_prefix, sys.dont_write_bytecode\n"
                        "try:\n    main(['--backend', 'local', 'clean'], standalone_mode=False)\n"
                        "except click.ClickException as error:\n"
                        f"    assert {failure!r} and 'authored failure' in str(error), error\n"
                        f"else:\n    assert not {failure!r}\n"
                        "assert (sys.pycache_prefix, sys.dont_write_bytecode) == previous\n"
                        "assert not Path(Path('loading-cache').read_text()).parent.exists()\n")
                    result = subprocess.run([sys.executable, "-c", script], cwd=work,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SourceFreshnessTests(LocalBackendTestCase):
    def configure_workflow(self):
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow, task_template\n"
            "@task_template\n"
            "def sample():\n"
            "    task = Task(inputs=[])\n"
            "    target = task.target('compute', inputs=[], outputs=['out'])\n"
            "    target << 'printf old > out'\n"
            "    task.retain('result', source=target.output('out'), path='out')\n"
            "    return task\n"
            "gwf = Workflow()\ngwf.task(sample())\n")

    def check_changed_command(self, source):
        self.run_complete()
        before = self.task_attempts()["sample"]
        self.assertEqual((self.work / "results/sample/out").read_text(), "old")
        cache, bytecode = replace_with_cached_source(self, source, "printf old", "printf new")
        self.assertRegex(self.cli("run", "--dry-run"), r"Task sample\s+Run\s+")
        for mode in ("--plain", "--static"):
            self.assertIn("fresh attempt required", self.cli("status", "sample", mode))
        task = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual(task["next_action"], "run")
        self.assertIn("changed commands: compute", task["reason"]["message"])
        self.assertIn("invalidated: changed commands: compute", self.cli("wait", success=False))
        terminal = TerminalSession([self.executable, "status", "sample"], cwd=self.work, size=(120, 30))
        self.addCleanup(terminal.close)
        terminal.wait_for("fresh attempt required")
        terminal.send("\r")
        terminal.wait_for("Reason: Changed commands")
        self.assertIn("Next action: Run", terminal.text)
        terminal.send("q")
        self.assertEqual(terminal.finish(), 0, terminal.text)
        self.run_complete()
        self.assertEqual((self.work / "results/sample/out").read_text(), "new")
        self.assertNotEqual(self.task_attempts()["sample"], before)
        self.assertIn("All awaited Task attempts are complete", self.cli("wait"))
        self.assertEqual(cache.read_bytes(), bytecode)

    def test_entry_point_command_edit_requires_fresh_attempt(self):
        self.check_changed_command(self.work / "workflow.py")

    def test_imported_task_command_edit_requires_fresh_attempt(self):
        workflow = self.work / "workflow.py"
        library = self.work / "tasks.py"
        declaration, registration = workflow.read_text().split("gwf = Workflow()")
        library.write_text(declaration)
        workflow.write_text("from gwflow import Workflow\nfrom tasks import sample\n"
                            "gwf = Workflow()" + registration)
        self.check_changed_command(library)
