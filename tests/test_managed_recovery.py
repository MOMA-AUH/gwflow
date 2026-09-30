"""Frontend coordination and conservative admission through backend fixtures."""

import json
import os
import shutil
import signal
import subprocess

from support import FIXTURES, GWF
import test_managed


# Reuse the fixture setup without inheriting another test class's test methods.
from support import LocalBackendTestCase


class ManagedCoordinationTests(LocalBackendTestCase):
    configure_workflow = test_managed.ManagedCliTests.configure_workflow
    write_task = test_managed.ManagedCliTests.write_task
    settle = test_managed.ManagedCliTests.settle

    def inject(self, **options):
        shutil.copy(FIXTURES / "recovery_backend.py", self.work)
        metadata = self.work / "recovery_backend-1.0.dist-info"
        metadata.mkdir(exist_ok=True)
        (metadata / "entry_points.txt").write_text("[gwf.backends]\nrecovery_fixture = recovery_backend:setup\n")
        self.configure(**{"backend.recovery_fixture.port": self.port})
        (self.work / "injection.json").write_text(json.dumps(options))
        return {**os.environ, "PYTHONPATH": str(self.work)}

    def launch(self, *command, env=None, filename="command-output"):
        output = self.work / filename
        with output.open("w") as stream:
            process = subprocess.Popen([GWF, *command], cwd=self.work, env=env,
                                       stdout=stream, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop_worker, process)
        return process, output

    def test_lost_acknowledgement_blocks_duplicate_execution(self):
        trace = self.work / "executions"
        import shlex
        self.write_task(f"echo executed >> {shlex.quote(str(trace))}; touch out.txt")
        env = self.inject(lose_ack="sample__write")
        self.assertIn("lost acknowledgement", self.cli("-b", "recovery_fixture", "run", env=env, success=False))
        self.settle()
        self.assertIn("unresolved submission", self.cli("explain"))
        for flags in ((), ("--force",)):
            self.assertIn("unresolved submission", self.cli("run", *flags, success=False))
        self.assertEqual(trace.read_text(), "executed\n")
        self.assertFalse((self.work / "results/sample").exists())

    def test_status_waits_until_submission_tracking_is_saved(self):
        submitter, submitted = self.launch("-b", "recovery_fixture", "run",
                                          env=self.inject(hold_tracking=True), filename="submitted")
        self.wait_for(lambda: (self.work / "tracking-held").exists())
        inspector, output = self.launch("status", "--details", filename="status")
        try:
            self.wait_for(lambda: "Waiting" in output.read_text() or inspector.poll() is not None)
            self.assertIsNone(inspector.poll(), output.read_text())
            self.assertNotIn("Task sample", output.read_text())
        finally:
            (self.work / "tracking-release").touch()
        submitter.wait(timeout=10)
        inspector.wait(timeout=10)
        self.assertEqual(submitter.returncode, 0, submitted.read_text())
        self.assertEqual(inspector.returncode, 0, output.read_text())
        self.finish()
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_interrupted_inspection_releases_guard_without_changing_records(self):
        self.run_complete()
        evidence = {p: p.read_bytes() for p in (self.work / ".gwf/gwflow").rglob("*.json")}
        inspector, output = self.launch("-b", "recovery_fixture", "explain", env=self.inject(hold_observation=True))
        self.wait_for(lambda: (self.work / "observation-held").exists())
        inspector.send_signal(signal.SIGINT)
        inspector.wait(timeout=10)
        self.assertNotEqual(inspector.returncode, 0)
        self.assertEqual({p: p.read_bytes() for p in evidence}, evidence)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_completion_resources_overlay_workflow_defaults(self):
        self.write_task("touch out.txt", settings="defaults={'cores':3, 'memory':'8g'}, completion_defaults={'cores':1, 'memory':None}")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("Task(inputs=[])", "Task(inputs=[], defaults={'cores':7})"))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(capture_options=True))
        self.finish()
        options = [json.loads(line) for line in (self.work / "submitted-options.jsonl").read_text().splitlines()]
        compute = next(item["options"] for item in options if item["name"].startswith("sample__write__"))
        completion = next(item["options"] for item in options if item["name"].startswith("sample__gwflow_complete__"))
        self.assertEqual(compute, {"cores":7, "memory":"8g"})
        self.assertEqual(completion, {"cores":1})
