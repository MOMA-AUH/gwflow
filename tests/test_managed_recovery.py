"""Frontend coordination and conservative admission through backend fixtures."""

import json
import os
import shutil
import signal
import subprocess
import sys
import shlex

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

    def test_saved_tracking_recovers_lost_acknowledgement_without_duplicate_execution(self):
        trace = self.work / "executions"
        import shlex
        self.write_task(f"echo executed >> {shlex.quote(str(trace))}; touch out.txt")
        env = self.inject(lose_ack="sample__write")
        self.assertIn("lost acknowledgement", self.cli("-b", "recovery_fixture", "run", env=env, success=False))
        self.settle()
        before = {p: p.read_bytes() for p in (self.work / ".gwf/gwflow").rglob("*.json")}
        self.assertIn("continue", self.cli("explain"))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.cli("run")
        self.finish()
        self.assertEqual(trace.read_text(), "executed\n")
        self.assertTrue((self.work / "results/sample/result.txt").exists())
        self.assertIn("reuse", self.cli("explain"))

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

    def test_proven_pre_admission_rejection_can_continue(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        self.assertIn("continue", self.cli("explain"))
        self.run_complete()
        self.assertTrue((self.work / "results/sample/report.txt").exists())

    def test_acknowledgement_recovers_after_frontend_dies_before_tracking_flush(self):
        trace, held, release = (self.work / name for name in ("executions", "held", "release"))
        self.write_task(f"echo executed >> {shlex.quote(str(trace))}; touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; touch out.txt")
        submitter, output = self.launch("-b", "recovery_fixture", "run", env=self.inject(hold_tracking=True))
        self.wait_for(lambda: (self.work / "tracking-held").exists())
        self.wait_for(held.exists)
        submitter.kill()
        submitter.wait(timeout=10)
        try:
            self.assertIn("active", self.cli("explain"))
            self.assertNotIn("Submitted target", self.cli("run"))
            self.assertIn("active work", self.cli("run", "--force", success=False))
        finally:
            release.touch()
        self.finish()
        self.assertIn("reuse", self.cli("explain"))
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(trace.read_text(), "executed\n")

    def test_interruption_after_intent_before_admission_stays_uncertain(self):
        submitter, output = self.launch("-b", "recovery_fixture", "run",
                                          env=self.inject(hold_submission_prefix="sample__gwflow_prepare"))
        self.wait_for(lambda: (self.work / "submission-held").exists())
        submitter.kill()
        submitter.wait(timeout=10)
        for flags in ((), ("--force",), ("--dry-run",)):
            diagnostic = self.cli("run", *flags, success=False)
            self.assertIn("Task sample", diagnostic)
            self.assertIn("unresolved submission: sample__gwflow_prepare__", diagnostic)
        self.assertFalse((self.work / "results/sample").exists())

    def test_arbitrary_backend_exception_does_not_prove_rejection(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_prefix="sample__gwflow_prepare"), success=False)
        self.assertIn("unresolved submission", self.cli("run", success=False))
        self.assertFalse((self.work / "results/sample").exists())

    def slurm_environment(self):
        binaries = self.work / "slurm-bin"
        binaries.mkdir()
        implementation = (FIXTURES / "slurm_command.py").read_text()
        for command in ("sbatch", "squeue", "sacct", "scancel"):
            executable = binaries / command
            executable.write_text(f"#!{sys.executable}\n" + implementation)
            executable.chmod(0o700)
        return {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                "GWF_SLURM_FIXTURE_ROOT": str(self.work), "GWF_SLURM_FIXTURE_PORT": str(self.port)}

    def test_pinned_slurm_dependency_contract_and_expired_history_reuse(self):
        env = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=env)
        self.finish()
        submitted = [json.loads(line) for line in (self.work / "slurm-submitted.jsonl").read_text().splitlines()]
        self.assertEqual(len(submitted), 3)
        prep, compute, completion = submitted
        self.assertEqual(prep["args"], ["--parsable"])
        self.assertEqual(compute["args"], ["--parsable", "--dependency=afterok:" + prep["id"]])
        self.assertEqual(completion["args"], ["--parsable", "--dependency=afterok:" + compute["id"]])
        self.assertIn("#SBATCH --job-name=" + compute["name"], compute["script"])
        self.assertEqual((self.work / "results/sample/report.txt").read_text(), "hello")
        (self.work / "forgotten-slurm-history").touch()
        self.assertIn("reuse", self.cli("-b", "slurm", "explain", env=env))
        self.assertNotIn("Submitted target", self.cli("-b", "slurm", "run", env=env))

    def test_slurm_wrapper_failure_cannot_publish_completion(self):
        self.write_task("printf unchecked > out.txt; exit 7")
        env = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=env)
        self.settle()
        submitted = [json.loads(line) for line in (self.work / "slurm-submitted.jsonl").read_text().splitlines()]
        compute = next(item for item in submitted if item["name"].startswith("sample__write__"))
        state = subprocess.run([str(self.work / "slurm-bin/sacct"), "--jobs", compute["id"]],
                               env=env, capture_output=True, text=True, check=True).stdout
        self.assertIn("FAILED", state)
        self.assertFalse((self.work / "results/sample").exists())
        self.assertIn("Task sample: retry;", self.cli("-b", "slurm", "explain", env=env))

    def test_slurm_acknowledgement_survives_frontend_loss_before_tracking(self):
        held, release = self.work / "held", self.work / "release"
        self.write_task(f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf checked > out.txt")
        env = self.slurm_environment()
        submitter = subprocess.run([
            sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "after_computation_ack", "sample",
            "-b", "slurm", "run",
        ], cwd=self.work, capture_output=True, text=True, env=env, timeout=30)
        try:
            self.assertEqual(submitter.returncode, 109, submitter.stdout + submitter.stderr)
            self.wait_for(held.exists)
            output = self.cli("-b", "slurm", "run", env=env)
            self.assertNotIn("Submitted target sample__write", output)
            self.assertIn("Submitted target sample__gwflow_complete", output)
            self.assertIn("active work", self.cli("-b", "slurm", "run", "--force", env=env, success=False))
        finally:
            release.touch()
        self.finish()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "checked")
        self.assertEqual(len((self.work / "slurm-submitted.jsonl").read_text().splitlines()), 3)

    def test_cancellation_request_and_unknown_status_do_not_prove_inactivity(self):
        env = self.slurm_environment()
        held, release = self.work / "held", self.work / "release"
        self.write_task(f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; touch out.txt")
        self.cli("-b", "slurm", "run", env=env)
        self.wait_for(held.exists)
        try:
            self.cli("-b", "slurm", "run", env=env)
            submitted = [json.loads(line) for line in (self.work / "slurm-submitted.jsonl").read_text().splitlines()]
            subprocess.run([str(self.work / "slurm-bin/scancel"), "--verbose", submitted[1]["id"]], env=env, check=True)
            self.assertIn("active work", self.cli("-b", "slurm", "run", "--force", env=env, success=False))
            (self.work / "forgotten-slurm-history").touch()
            self.assertIn("unresolved submission", self.cli("-b", "slurm", "run", env=env, success=False))
            self.assertEqual(len((self.work / "slurm-submitted.jsonl").read_text().splitlines()), 3)
        finally:
            release.touch()
        self.finish()
        self.assertIn("reuse", self.cli("-b", "slurm", "explain", env=env))

    def test_execution_evidence_resolves_acceptance_after_tracking_and_ack_are_lost(self):
        held, release, trace = (self.work / name for name in ("held", "release", "executions"))
        self.write_task(f"echo executed >> {shlex.quote(str(trace))}; touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; touch out.txt")
        self.cli("-b", "recovery_fixture", "run", env=self.inject(lose_tracking="sample__write"), success=False)
        self.wait_for(held.exists)
        try:
            self.assertIn("unresolved submission: sample__write__", self.cli("run", success=False))
        finally:
            release.touch()
        self.settle()
        self.assertIn("continue", self.cli("explain"))
        self.run_complete()
        self.assertEqual(trace.read_text(), "executed\n")
        self.assertIn("reuse", self.cli("explain"))

    def test_missing_current_intent_does_not_resubmit_archived_admission(self):
        self.run_complete()
        # Intent deletion is an explicit persistence fault, not a way to
        # inspect ordinary lifecycle behavior.
        for path in (self.work / ".gwf/gwflow").rglob("*-intent.json"):
            value = json.loads(path.read_text())
            if "__write__" in value.get("submission", ""):
                path.unlink()
                break
        else:
            self.fail("No computation intent was available for fault injection")
        self.assertIn("unresolved submission", self.cli("run", success=False))
        self.assertEqual((self.work / "results/sample/report.txt").read_text(), "hello")

    def preparation_gate(self, **backend_options):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_before_preparation": True}))
        return self.inject(job_fault="sample__gwflow_prepare", **backend_options)

    def test_admitted_active_preparation_can_submit_remaining_dependencies(self):
        env = self.preparation_gate(lose_ack="sample__gwflow_prepare")
        self.cli("-b", "recovery_fixture", "run", env=env, success=False)
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        try:
            submitted = self.cli("run")
            self.assertIn("Submitted target sample__write__", submitted)
            self.assertNotIn("Submitted target sample__gwflow_prepare__", submitted)
            self.assertFalse((self.work / "results/sample").exists())
        finally:
            (self.work / "preparation-release").touch()
        self.finish()
        self.assertIn("reuse", self.cli("explain"))

    def test_confirmed_cancelled_preparation_can_retry(self):
        from gwf.backends.local import Client, LocalStatus
        self.cli("-b", "recovery_fixture", "run", env=self.preparation_gate())
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        with Client.connect(port=self.port) as client:
            running = [job_id for job_id, state in client.status().items() if state == LocalStatus.RUNNING]
            self.assertEqual(len(running), 1)
            client.cancel(int(running[0]))
        self.settle()
        self.assertIn("restart interrupted preparation", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/report.txt").read_text(), "hello")
        self.assertIn("reuse", self.cli("explain"))

    def test_missing_initialization_ready_evidence_blocks_admission(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(reject_before_admission=True), success=False)
        for path in (self.work / ".gwf/gwflow").rglob("*.json"):
            if json.loads(path.read_text()).get("kind") == "gwflow.ready":
                path.unlink()
                break
        else:
            self.fail("No initialization evidence was available for fault injection")
        self.assertIn("initialization", self.cli("run", success=False))
        self.assertFalse((self.work / "results/sample").exists())

    def test_unreadable_archive_cannot_make_an_admission_look_unsubmitted(self):
        self.run_complete()
        paths = []
        for path in (self.work / ".gwf/gwflow").rglob("*.json"):
            record = json.loads(path.read_text())
            if record.get("kind") == "gwflow.submission-intent" and "__write__" in record.get("submission", ""):
                paths.append(path)
        self.assertEqual(len(paths), 2)
        for path in paths:
            if path.name.endswith("-intent.json"):
                path.unlink()
            else:
                path.write_text("{truncated")
        self.assertIn("unresolved submission", self.cli("run", success=False))
        self.assertEqual((self.work / "results/sample/report.txt").read_text(), "hello")
