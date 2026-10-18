"""The installed release's log, cancellation, and recovery user journey."""

from collections import Counter
import re
import shlex

from support import LocalBackendTestCase
import test_graphs


class TroubleshootingJourneyTests(LocalBackendTestCase):
    settle = test_graphs.TaskGraphTests.settle

    def configure_workflow(self):
        failing, held, release, trace = (self.work / name for name in ("fail-right", "held", "release", "trace"))
        failing.touch()
        test_graphs.TaskGraphTests.configure_workflow(self, right_command=(
            f"echo right >> {shlex.quote(str(trace))}; "
            f"if [ -e {shlex.quote(str(failing))} ]; then echo right-failed >&2; exit 8; fi; "
            f"touch {shlex.quote(str(held))}; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf right > same.txt"))
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                           "finished = empty_task(inputs=[])\n"
                           "write = finished.target('write', inputs=[], outputs=['saved.txt'])\n"
                           f"write << {'echo finished >> ' + shlex.quote(str(trace)) + '; printf saved > saved.txt'!r}\n"
                           "finished.retain('saved', source=write.output('saved.txt'), path='saved.txt')\n"
                           "gwf.task(finished, alias='finished')\n")

    def test_logs_retry_history_cancel_and_ordinary_recovery(self):
        self.cli("run")
        self.settle()
        index = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
        first = re.search(r"sample__right: failed; submission (\S+)", index).group(1)
        command = next(shlex.split(line.strip()) for line in index.splitlines()
                       if "logs " + first in line and "--stderr" in line)
        original_error = self.cli(*command[1:])
        self.assertIn("right-failed", original_error)
        self.assertIn("Retry", self.cli("run", "sample", "--dry-run", "--details"))
        saved = self.work / "results/finished/saved.txt"
        retained = (saved.read_bytes(), saved.stat().st_mtime_ns)

        (self.work / "fail-right").unlink()
        self.cli("run", "sample")
        try:
            self.wait_for((self.work / "held").exists)
            current = self.cli("logs", "sample", "--details")
            second = re.search(r"sample__right: running; submission (\S+)", current).group(1)
            self.assertNotEqual(first, second)
            self.assertNotIn(first, current)
            self.assertIn(attempt, current)
            history = self.cli("logs", "sample", "--history", "--details")
            self.assertIn(f"sample__right: failed (historical); submission {first}", history)
            self.assertIn(second, history)
            preview = self.cli("cancel", "sample", "--dry-run")
            self.assertEqual(preview.count("Would request cancellation"), 3, preview)
            self.assertIn(second, preview)
            self.assertNotIn("Task finished:", preview)
            requested = self.cli("cancel", "sample")
            self.assertEqual(requested.count("Cancellation requested"), 3, requested)
            self.assertIn("may still be stopping", requested)
            self.settle()
            self.assertEqual((saved.read_bytes(), saved.stat().st_mtime_ns), retained)
        finally:
            (self.work / "release").touch()

        self.assertIn("Retry", self.cli("run", "sample", "--dry-run"))
        self.cli("run", "sample")
        self.settle()
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertEqual((saved.read_bytes(), saved.stat().st_mtime_ns), retained)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()),
                         {"left": 1, "right": 3, "join": 1, "finished": 1})
        current = self.cli("logs", "sample", "--details")
        self.assertIn(attempt, current)
        self.assertNotIn(first, current)
        self.assertNotIn(second, current)
        history = self.cli("logs", "sample", "--history", "--details")
        self.assertIn(first, history)
        self.assertIn(second, history)
        self.assertEqual(self.cli(*command[1:]), original_error)
