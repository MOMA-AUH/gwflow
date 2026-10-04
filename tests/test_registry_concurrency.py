"""Independent frontend processes safely share registry acquisition."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from support import GWF
import test_registry_images


class RegistryConcurrencyTests(test_registry_images.RegistryTestCase):
    def planner(self, name):
        work = self.work / name
        work.mkdir()
        marker = self.controller / (name + "-loaded")
        source = (self.work / "workflow.py").read_text().replace("'sample'", repr(name))
        (work / "workflow.py").write_text(source + f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        shutil.copyfile(self.work / ".gwfconf.json", work / ".gwfconf.json")
        process = subprocess.Popen([GWF, "run", "--dry-run"], cwd=work, start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop_planner, process)
        self.wait_for(marker.exists)
        return process

    def stop_planner(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)

    def completed_plan(self, process, *, success=True):
        output, _ = process.communicate(timeout=30)
        if success:
            self.assertEqual(process.returncode, 0, output)
        else:
            self.assertNotEqual(process.returncode, 0, output)
        self.assertNotIn("Submitted target", output)
        return output

    def release(self, name):
        (self.controller / (name + "-release")).touch()

    def test_two_workflows_wait_for_one_complete_acquisition(self):
        self.options(gate="shared")
        owner = self.planner("first")
        self.wait_for(lambda: (self.controller / "shared-held").exists())
        try:
            waiter = self.planner("second")
            # Both workflows have loaded. While the external pull is held,
            # no planner may return using the private partial image.
            time.sleep(1)
            self.assertIsNone(owner.poll())
            self.assertIsNone(waiter.poll())
            self.assertEqual(self.cached_images(), [])
        finally:
            self.release("shared")
        self.completed_plan(owner)
        self.completed_plan(waiter)
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertEqual(len(self.calls("pull-success")), 1)
        self.assertEqual(len(self.cached_images()), 1)
        for name in ("first", "second"):
            self.assertFalse((self.work / name / ".gwf/gwflow").exists())
        self.run_complete()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")
        self.assertEqual(Path(self.calls("exec")[0]["image"]), self.cached_images()[0])
        self.assertEqual(len(self.calls("pull")), 1)

    def test_waiter_recovers_after_owner_pull_fails(self):
        self.options(gate="failure", fail=True)
        owner = self.planner("failed_owner")
        self.wait_for(lambda: (self.controller / "failure-held").exists())
        try:
            self.options()
            waiter = self.planner("recovering_waiter")
            time.sleep(1)
            self.assertIsNone(waiter.poll())
            self.assertEqual(self.cached_images(), [])
        finally:
            self.release("failure")
        output = self.completed_plan(owner, success=False)
        for text in ("failed_owner", "compute", self.reference, "fixture registry unavailable", "No jobs will be submitted"):
            self.assertIn(text, output)
        self.completed_plan(waiter)
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(len(self.calls("pull-success")), 1)
        self.assertEqual(len(self.cached_images()), 1)
        self.cli("run", "--dry-run")
        self.assertEqual(len(self.calls("pull")), 2)

    def test_waiter_recovers_after_killed_owner_and_preserves_other_images(self):
        self.cli("explain")
        published = self.cached_images()[0]
        before = published.read_bytes(), published.stat().st_mtime_ns, published.stat().st_ino
        self.configure_workflow("docker://example.org/tools/interrupted:v1")
        self.options(gate="interrupted")
        owner = self.planner("killed_owner")
        self.wait_for(lambda: (self.controller / "interrupted-held").exists())
        try:
            waiter = self.planner("surviving_waiter")
            time.sleep(1)
            self.assertIsNone(waiter.poll())
            self.options()
            # Kill only the frontend first. Its still-gated external pull
            # must not retain acquisition ownership or prevent the waiter.
            owner.kill()
            owner.communicate(timeout=10)
            self.completed_plan(waiter)
        finally:
            try:
                os.killpg(owner.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.release("interrupted")
        self.assertEqual(len(self.calls("pull")), 3)
        self.assertEqual(len(self.calls("pull-success")), 2)
        self.assertEqual(len(self.cached_images()), 2)
        self.assertEqual((published.read_bytes(), published.stat().st_mtime_ns, published.stat().st_ino), before)
        self.run_complete()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")
        self.assertEqual(len(self.calls("pull")), 3)

    def test_waiting_failed_callers_leave_a_retryable_miss(self):
        self.options(gate="unavailable", fail=True)
        owner = self.planner("first_failure")
        self.wait_for(lambda: (self.controller / "unavailable-held").exists())
        try:
            waiter = self.planner("second_failure")
            time.sleep(1)
            self.assertIsNone(waiter.poll())
        finally:
            self.release("unavailable")
        for process in (owner, waiter):
            output = self.completed_plan(process, success=False)
            self.assertIn("fixture registry unavailable", output)
            self.assertIn("No jobs will be submitted", output)
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(self.calls("pull-success"), [])
        self.assertEqual(self.cached_images(), [])
        self.options()
        self.run_complete()
        self.assertEqual(len(self.calls("pull")), 3)
        self.assertEqual(len(self.calls("pull-success")), 1)
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")

    def test_distinct_references_can_acquire_while_another_pull_waits(self):
        self.options(gate="independent")
        owner = self.planner("slow_image")
        self.wait_for(lambda: (self.controller / "independent-held").exists())
        try:
            self.options()
            self.configure_workflow("docker://example.org/tools/independent:v1")
            other = self.planner("other_image")
            self.completed_plan(other)
            self.assertIsNone(owner.poll())
            self.assertEqual(len(self.calls("pull-success")), 1)
            self.assertEqual(len(self.cached_images()), 1)
        finally:
            self.release("independent")
        self.completed_plan(owner)
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(len(self.calls("pull-success")), 2)
        self.assertEqual(len(self.cached_images()), 2)
