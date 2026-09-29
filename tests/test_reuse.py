"""Completion and reuse through the installed API and ordinary gwf CLI."""

from collections import Counter
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from gwf.backends.local import Client, LocalStatus


GWF = str(Path(sys.executable).with_name("gwf"))
FIXTURES = Path(__file__).parent / "fixtures"


class LocalBackendTestCase(unittest.TestCase):
    workers = 2

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow reuse ")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        (self.work / "input.txt").write_text("hello\n")
        (self.work / "extra.txt").write_text("extra\n")
        self.configure_workflow()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.config = {"backend": "local", "backend.local.port": self.port}
        self.configure()
        worker = subprocess.Popen(
            [GWF, "workers", "-n", str(self.workers), "-p", str(self.port)], cwd=self.work,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_worker, worker)
        self.wait_for(lambda: self.worker_ready(worker))

    def worker_ready(self, worker):
        if worker.poll() is not None:
            self.fail(f"gwf workers exited: {worker.stderr.read()}")
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    def stop_worker(self, worker):
        worker.terminate()
        try:
            worker.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate(timeout=10)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for local jobs")

    def configure(self, **options):
        self.config.update(options)
        (self.work / ".gwfconf.json").write_text(json.dumps(self.config))

    def state_backend(self, states):
        shutil.copy(FIXTURES / "state_backend.py", self.work)
        metadata = self.work / "state_backend-1.0.dist-info"
        metadata.mkdir(exist_ok=True)
        (metadata / "entry_points.txt").write_text("[gwf.backends]\nstate_fixture = state_backend:setup\n")
        (self.work / "backend-state.json").write_text(json.dumps(states))
        return {**os.environ, "PYTHONPATH": str(self.work)}

    def configure_workflow(self, **options):
        shutil.copy(FIXTURES / "reuse_task.py", self.work)
        (self.work / "definition.json").write_text(json.dumps(options))
        (self.work / "workflow.py").write_text(
            "import json\n"
            "from pathlib import Path\n"
            "from gwflow import Workflow\n"
            "from reuse_task import text_task\n"
            "gwf = Workflow()\n"
            "gwf.task_from_template('text', text_task(**json.loads(Path('definition.json').read_text())))\n"
        )

    def cli(self, *args, success=True, env=None):
        result = subprocess.run(
            [GWF, *args], cwd=self.work, capture_output=True, text=True,
            timeout=30, env=env,
        )
        output = result.stdout + result.stderr
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        return output

    def finish(self):
        def done():
            with Client.connect(port=self.port) as client:
                states = client.status()
            self.assertFalse(any(state == LocalStatus.FAILED for state in states.values()), states)
            return states and all(state == LocalStatus.COMPLETED for state in states.values())
        self.wait_for(done)

    def run_complete(self):
        output = self.cli("run")
        self.finish()
        return output

    def trace(self):
        return Counter((self.work / "trace.txt").read_text().splitlines())


class ReuseCliTests(LocalBackendTestCase):
    def clean_intermediate(self):
        (self.work / "middle.txt").unlink()

    def assert_reused(self):
        before = self.trace()
        output = self.cli("run")
        self.assertNotIn("Submitted target", output)
        self.assertEqual(self.trace(), before)
        self.assertFalse((self.work / "middle.txt").exists())

    def test_completed_task_survives_cleanup_and_keeps_logs(self):
        first = self.run_complete()
        self.assertIn("Submitted target text__gwflow_complete", first)
        self.assertEqual((self.work / "result.txt").read_text(), "HELLO\n")
        self.clean_intermediate()
        config = (self.work / ".gwfconf.json").read_bytes()
        logs = {path.name: path.read_bytes() for path in (self.work / ".gwf" / "logs").iterdir()}
        self.assert_reused()
        self.assert_reused()
        self.assertEqual((self.work / ".gwfconf.json").read_bytes(), config)
        self.assertEqual(logs, {path.name: path.read_bytes() for path in (self.work / ".gwf" / "logs").iterdir()})
        self.assertIn("prepare log", self.cli("logs", "text__prepare", "--no-pager"))

    def test_finalizer_waits_for_nonretained_side_branch(self):
        self.configure_workflow(side_gate=True)
        self.cli("run")
        self.wait_for(lambda: (self.work / "result.txt").exists())
        self.wait_for(lambda: (self.work / "side_started").exists())
        self.assertFalse((self.work / ".gwf" / "logs" / "text__gwflow_complete.stdout").exists())
        self.assertNotIn("Submitted target", self.cli("run"))
        (self.work / "release").touch()
        self.finish()
        self.assertTrue((self.work / ".gwf" / "logs" / "text__gwflow_complete.stdout").exists())
        self.clean_intermediate()
        (self.work / "side.txt").unlink()
        self.assert_reused()

    def test_missing_external_input_is_reported_even_after_completion(self):
        self.run_complete()
        self.clean_intermediate()
        (self.work / "input.txt").unlink()
        output = self.cli("run", success=False)
        self.assertIn("does not exist", output)
        self.assertIn("input.txt", output)
        self.assertEqual(self.trace(), {"prepare": 1, "finish": 1})

    def test_unused_declared_external_input_is_still_checked(self):
        self.configure_workflow(boundary_extra=True)
        self.run_complete()
        self.clean_intermediate()
        (self.work / "extra.txt").unlink()
        self.assertIn("extra.txt", self.cli("run", success=False))

    def test_missing_retained_output_uses_partial_ordinary_rerun(self):
        self.run_complete()
        (self.work / "result.txt").unlink()
        output = self.run_complete()
        self.assertNotIn("Submitted target text__prepare", output)
        self.assertIn("Submitted target text__finish", output)
        self.assertEqual(self.trace(), {"prepare": 1, "finish": 2})
        self.clean_intermediate()
        self.assert_reused()

    def test_equal_mtimes_and_content_changes_do_not_invalidate_reuse(self):
        self.run_complete()
        self.clean_intermediate()
        stamp = (self.work / "result.txt").stat().st_mtime_ns
        (self.work / "input.txt").write_text("a different size and content\n")
        os.utime(self.work / "input.txt", ns=(stamp, stamp))
        self.assert_reused()

    def test_strictly_newer_input_prevents_reuse(self):
        self.run_complete()
        self.clean_intermediate()
        old = (self.work / "input.txt").stat().st_mtime_ns - 2_000_000_000
        os.utime(self.work / "result.txt", ns=(old, old))
        self.run_complete()
        self.assertEqual(self.trace(), {"prepare": 2, "finish": 2})

    def test_missing_completion_evidence_never_reuses(self):
        self.run_complete()
        self.clean_intermediate()
        shutil.rmtree(self.work / ".gwf" / "gwflow")
        self.run_complete()
        self.assertEqual(self.trace(), {"prepare": 2, "finish": 2})

    def test_invalid_completion_evidence_recovers_without_command_tracking(self):
        self.run_complete()
        # Corrupt only published records, keeping the expected attempt intact.
        for path in (self.work / ".gwf" / "gwflow").rglob("*.json"):
            if path.name != "expected.json":
                path.write_text("not JSON")
        output = self.run_complete()
        self.assertIn("Submitted target text__gwflow_complete", output)
        self.assertEqual(self.trace(), {"prepare": 1, "finish": 1})
        self.clean_intermediate()
        self.assert_reused()

    def test_command_only_change_follows_tracking_policy(self):
        for tracking in (None, False, True):
            with self.subTest(tracking=tracking):
                if tracking is not None:
                    self.configure(use_spec_hashes=tracking)
                self.configure_workflow(command_suffix=str(tracking))
                self.run_complete()
                self.clean_intermediate()
                self.configure_workflow(command_suffix=f"changed {tracking}")
                if tracking:
                    output = self.run_complete()
                    self.assertIn("Submitted target text__prepare", output)
                    self.clean_intermediate()
                self.assert_reused()
                # Make the next policy case begin with ordinary work.
                (self.work / "result.txt").unlink()

    def test_enabled_tracking_checks_saved_inner_hashes(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        self.clean_intermediate()
        # Model missing command history through gwf's own saved-hash API.
        from gwf import Target
        from gwf.conf import FileConfig
        from gwf.core import get_spec_hashes
        with get_spec_hashes(working_dir=str(self.work), config=FileConfig.load(self.work / ".gwfconf.json")) as hashes:
            hashes.invalidate(Target("text__prepare", [], [], {}))
        output = self.run_complete()
        self.assertIn("Submitted target text__prepare", output)
        self.assertEqual(self.trace(), {"prepare": 2, "finish": 2})

    def test_ordering_versions_and_resources_preserve_reuse(self):
        for tracking in (False, True):
            with self.subTest(tracking=tracking):
                self.configure(use_spec_hashes=tracking)
                original = dict(boundary_extra=True, inner_extra=True, side=True,
                                retain_side=True, extra_output=True)
                self.configure_workflow(**original)
                self.run_complete()
                self.clean_intermediate()
                self.configure_workflow(**original, reverse=True, version="2.0", cores=4)
                self.assert_reused()
                (self.work / "result.txt").unlink()

    def test_changed_declarations_and_membership_expose_ordinary_targets(self):
        changes = (
            ({}, {"boundary_extra": True}),
            ({"side": True}, {"side": True, "retain_side": True}),
            ({"boundary_extra": True}, {"inner_extra": True, "boundary_extra": True}),
            ({}, {"extra_output": True}),
            ({}, {"prepare_name": "renamed"}),
            ({}, {"side": True}),
        )
        for original, change in changes:
            with self.subTest(change=change):
                self.configure_workflow(**original)
                self.run_complete()
                self.clean_intermediate()
                before = self.trace()
                self.configure_workflow(**change)
                output = self.run_complete()
                self.assertIn("Submitted target text__", output)
                self.assertEqual(self.trace()["prepare"], before["prepare"] + 1)
                self.assertTrue((self.work / "middle.txt").exists())

    def test_removing_a_target_prevents_reuse(self):
        self.configure_workflow(side=True)
        self.run_complete()
        self.clean_intermediate()
        self.configure_workflow()
        self.run_complete()
        self.assertEqual(self.trace(), {"prepare": 2, "finish": 2, "side": 1})

    def test_disabling_tracking_ignores_commands_in_older_records(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        self.clean_intermediate()
        self.configure(use_spec_hashes=False)
        self.configure_workflow(command_suffix="new command")
        self.assert_reused()

    def test_obsolete_completion_record_does_not_satisfy_new_attempt(self):
        self.run_complete()
        records = self.work / ".gwf" / "gwflow"
        old_paths = {path for path in records.rglob("*.json") if path.name != "expected.json"}
        old_record = next(iter(old_paths)).read_bytes()
        (self.work / "result.txt").unlink()
        self.run_complete()
        new_paths = {path for path in records.rglob("*.json") if path.name != "expected.json"} - old_paths
        self.assertTrue(new_paths)
        for path in new_paths:
            path.write_bytes(old_record)
        self.clean_intermediate()
        self.run_complete()
        self.assertEqual(self.trace(), {"prepare": 2, "finish": 3})

    def test_validation_still_checks_completed_task_before_omission(self):
        self.run_complete()
        self.clean_intermediate()
        with (self.work / "workflow.py").open("a") as stream:
            stream.write("gwf.target('duplicate', inputs=[], outputs=['result.txt']) << 'touch forbidden'\n")
        self.assertIn("provided by targets", self.cli("run", success=False))
        self.assertFalse((self.work / "forbidden").exists())
        self.assertEqual(self.trace(), {"prepare": 1, "finish": 1})

    def test_backend_states_gate_omission_with_existing_evidence(self):
        self.run_complete()
        self.clean_intermediate()
        # gwf's backend plugin seam supplies otherwise transient states while
        # retaining valid on-disk evidence. It never submits fixture work.
        env = self.state_backend({})
        for state in ("SUBMITTED", "RUNNING", "FAILED", "CANCELLED", "UNKNOWN", "COMPLETED"):
            for target in ("text__prepare", "text__gwflow_complete"):
                with self.subTest(state=state, target=target):
                    (self.work / "backend-state.json").write_text(json.dumps({target: state}))
                    output = self.cli("-b", "state_fixture", "run", "--dry-run", env=env)
                    if state in ("UNKNOWN", "COMPLETED"):
                        self.assertNotIn("Would submit", output)
                    else:
                        self.assertIn("Would submit text__finish", output)
        self.assertEqual(self.trace(), {"prepare": 1, "finish": 1})
        self.assert_reused()


if __name__ == "__main__":
    unittest.main()
