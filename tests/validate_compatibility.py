"""Compare independent installed packages through unchanged public workflows."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import unittest

from gwf.backends.local import Client, LocalStatus, get_socket_path
from packaging.version import Version

from support import FIXTURES, LocalBackendTestCase
from compatibility_environment import disjoint_environments, installed_environment
from test_managed_recovery import ManagedCoordinationTests
from test_graphs import TaskGraphTests
from test_inputs import ExternalInputTests
from test_transfer import TransferRecoveryTests
from test_repair import ResultsRepairTests
from test_recovery import OperatorRecoveryTests
from fixtures.status_consumer_v1 import consume, check_handoff, check_notices


CONSUMER_REPORTS = {"cleaned-summary", "cleaned-details", "partial-summary", "partial-details",
                    "empty-filter", "usage-error", "inspection-error", "blocked", "unallocated"}


class InstalledCompatibilityTests(LocalBackendTestCase):
    def inject(self, **options):
        ManagedCoordinationTests.inject(self, **options)
        return {**self.environment, "PYTHONPATH": str(self.work)}

    def setUp(self):
        self.select_installation("baseline")
        super().setUp()

    def make_workdir(self):
        work = self.root / self._testMethodName
        work.mkdir()
        return work

    def select_installation(self, side):
        self.side = side
        self.executable = str(Path(self.installations[side]["executable"]).with_name("gwflow"))
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "CONDA_PREFIX", "VIRTUAL_ENV",
                                           "GWFLOW_COMPAT_CONSUMER", "GWFLOW_COMPAT_UNOWNED",
                                           "GWFLOW_COMPAT_DIAGNOSTICS", "GWFLOW_COMPAT_INSPECTION_ERROR")}
        self.environment["PYTHONNOUSERSITE"] = "1"
        self.environment["PATH"] = str(Path(self.executable).parent) + os.pathsep + self.environment.get("PATH", "")

    def start_worker(self):
        return super().start_worker(env=self.environment, start_new_session=True)

    def record(self, event):
        with (self.root / "transcript.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": time.time(), "test": self._testMethodName,
                                     "side": self.side, **event}) + "\n")

    def cli_result(self, *args, success=True, env=None):
        result = super().cli_result(*args, success=success, env=env or self.environment)
        self.record({"command": result.args, "cwd": str(self.work), "exit": result.returncode,
                     "stdout": result.stdout, "stderr": result.stderr})
        return result

    def logical_records(self, kind=None):
        records = {}
        for path in (self.work / ".gwf/gwflow").rglob("record-state.json"):
            state = json.loads(path.read_text())
            collection = state["records"]
            if state["phase"] == "individual":
                collection = {str(record.relative_to(path.parent)): json.loads(record.read_text())
                              for record in path.parent.rglob("*.json") if record != path}
            records.update({str(path.parent.relative_to(self.work) / name): value
                            for name, value in collection.items()
                            if kind is None or value.get("kind") == "gwflow." + kind})
        return records

    def snapshot(self):
        return {str(path.relative_to(self.work)): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
        } for directory in (".gwf/gwflow", "results")
                for path in (self.work / directory).rglob("*") if path.is_file()}

    def submission_identities(self, task):
        # Dynamic identities are observed before handoff; the reference consumer
        # independently fixes the expected field names and lifecycle meanings.
        report = json.loads(self.cli_result("status", task, "--json", "--details").stdout)
        return {job["name"]: (job["submission_name"], job["backend_id"])
                for job in report["tasks"][0]["jobs"]}

    def consume_status(self, label, *arguments, exit_code=0, env=None):
        self.assertEqual(self.side, "candidate")
        result = self.cli_result("status", "--json", *arguments, success=exit_code == 0, env=env)
        self.assertEqual(result.returncode, exit_code)
        report = consume(result.stdout, details="--details" in arguments, exit_code=result.returncode)
        self.consumer_reports.append(label)
        self.record({"reference_consumer_report": label})
        return report, result

    def consume_handoff(self, case, attempt_id, identities):
        name = "cleaned" if case == "cleaned" else "sample"
        for arguments, suffix in (((name,), "summary"), ((name, "--details"), "details")):
            report, _ = self.consume_status(case + "-" + suffix, *arguments)
            check_handoff(report, case=case, attempt_id=attempt_id, identities=identities)


class CompletedCompatibilityTests(InstalledCompatibilityTests):
    def configure_workflow(self):
        shutil.copy(FIXTURES / "compatibility_workflow.py", self.work / "workflow.py")

    def test_completed_cleaned_and_recovered_tasks_reuse_before_new_consumer(self):
        names = ("completed", "cleaned", "recovered")
        workflow = (self.work / "workflow.py").read_bytes()
        rejected = self.cli("-b", "recovery_fixture", "run", "recovered",
                            env=self.inject(reject_prefix="recovered__gwflow_prepare"), success=False)
        self.assertIn("injected submission failure", rejected)
        self.assertIn("Resolved submission", self.cli("recover", "recovered"))
        self.run_complete()
        self.cli("clean", "--delete", "cleaned")
        attempts = self.task_attempts()
        cleaned_identities = self.submission_identities("cleaned")
        for name in names:
            self.assertEqual((self.work / "results" / name / "text.txt").read_text(), "hello\n")
            self.assert_runtime(name, "baseline")
        self.assertEqual(self.trace(), Counter({name: 1 for name in names}))
        self.assertFalse(list((self.work / "work/cleaned").iterdir()))
        self.assertTrue(list((self.work / "work/completed").iterdir()))
        records = self.logical_records("operator-recovery")
        self.assertEqual(len(records), 1)
        before = self.snapshot()
        identities = {str(path): {"device": path.stat().st_dev, "inode": path.stat().st_ino}
                      for path in (self.work, self.work / "work", self.work / "results", self.work / ".gwf/gwflow")}
        self.finish()
        with Client.connect(get_socket_path(self.work)) as client:
            states = client.status()
            client.send("get_task_states")
            kind, response = client.recv()
        self.assertEqual(kind, "task_states")
        self.assertEqual(response.get("gwflow_termination_records"), 1)
        self.assertEqual(response.get("gwflow_process_tree"), 1)
        self.assertTrue(states and all(state == LocalStatus.COMPLETED for state in states.values()), states)
        self.stop_worker(self.worker)
        self.assertIsNotNone(self.worker.poll())
        # The managed scheduler inherits this isolated process group.
        # These controlled commands run synchronously and never detach. Group
        # absence therefore checks its descendants, not merely its exit code.
        with self.assertRaises(ProcessLookupError):
            os.killpg(self.worker.pid, 0)
        self.record({"baseline_jobs_completed": True, "worker_exit": self.worker.returncode,
                     "baseline_process_group_gone": True, "durable_termination": True,
                     "job_states": {str(job): state.name for job, state in states.items()},
                     "attempts": attempts, "storage": identities, "snapshot": before})

        bounded_receipts = (self.work / '.gwf/local-termination/records.json').is_file()
        self.select_installation("candidate")
        self.worker = self.start_worker()
        if not bounded_receipts:
            # #362 deliberately has no legacy receipt migration before 1.0.
            # The controlled synchronous jobs and process-group absence above
            # establish inactivity independently before explicit recovery.
            old_records = self.logical_records()
            for arguments in (("run", "--dry-run"), ("run",)):
                self.assertIn("cannot confirm computation descendants have stopped",
                              self.cli(*arguments, success=False))
                self.assertEqual(self.snapshot(), before)
            for name in names:
                self.assertIn("Resolved submission", self.cli("recover", name))
            recovered = self.logical_records()
            self.assertEqual({name: recovered.get(name) for name in old_records}, old_records)
            added = {name: record for name, record in recovered.items() if name not in old_records}
            self.assertTrue(added)
            self.assertEqual({record['kind'] for record in added.values()}, {'gwflow.operator-recovery'})
            after_recovery = self.snapshot()
            self.assertEqual({name: record for name, record in after_recovery.items() if name.startswith('results/')},
                             {name: record for name, record in before.items() if name.startswith('results/')})
            self.record({"legacy_termination_recovery": True, "added_recoveries": len(added),
                         "old_records_unchanged": True, "retained_outputs_unchanged": True})
            before = after_recovery
        self.cli("status", "--details")
        preview = self.cli("run", "--dry-run")
        for name in names:
            self.assertRegex(preview, rf"Task {name}\s+Reuse\s+")
        self.assertNoSubmissions(self.cli("run"))
        self.assertIn("No unresolved submissions", self.cli("recover", "recovered"))
        self.assertEqual(self.task_attempts(), attempts)
        self.assertEqual(self.snapshot(), before)
        self.consume_handoff("cleaned", attempts["cleaned"], cleaned_identities)
        noisy = {**self.environment, "GWFLOW_COMPAT_DIAGNOSTICS": "1"}
        empty, result = self.consume_status("empty-filter", "cleaned", "--status", "failed", env=noisy)
        self.assertEqual(empty["tasks"], [])
        check_notices(empty, {})
        for message in ("compatibility workflow diagnostic", "compatibility child diagnostic"):
            self.assertIn(message, result.stderr)
        error, _ = self.consume_status("usage-error", "--plain", exit_code=2)
        self.assertEqual(error["error"]["code"], "usage_error")
        error, result = self.consume_status("inspection-error", exit_code=1,
            env={**noisy, "GWFLOW_COMPAT_INSPECTION_ERROR": "1"})
        self.assertEqual(error["error"]["code"], "inspection_failed")
        for message in ("compatibility workflow diagnostic", "compatibility child diagnostic"):
            self.assertIn(message, result.stderr)
        source = self.work / "input.txt"
        source.rename(self.work / "saved-input.txt")
        try:
            blocked, _ = self.consume_status("blocked", "cleaned")
            task = blocked["tasks"][0]
            self.assertEqual((task["name"], task["attempt_id"], task["state"], task["next_action"],
                              task["reason"]["code"]),
                             ("cleaned", attempts["cleaned"], "blocked", "blocked", "input_unavailable"))
            check_notices(blocked, {"workflow_blocked": {"cleaned"}})
        finally:
            (self.work / "saved-input.txt").rename(source)
        self.assertEqual(self.snapshot(), before)
        environment = {**self.environment, "GWFLOW_COMPAT_CONSUMER": "1"}
        unallocated, _ = self.consume_status("unallocated", "consumer", "--details", env=environment)
        task = unallocated["tasks"][0]
        self.assertIsNone(task["attempt_id"])
        self.assertEqual((task["name"], task["state"], task["next_action"], task["progress"]["completed"],
                          task["progress"]["total"], task["reason"]["code"]),
                         ("consumer", "pending", "run", 0, 3, "no_attempt"))
        self.assertEqual({job["name"] for job in task["jobs"]},
                         {"consumer__gwflow_prepare", "consumer__join", "consumer__gwflow_complete"})
        for job in task["jobs"]:
            self.assertEqual((job["submission_name"], job["backend_id"], job["observed_state"]), (None, None, None))
            self.assertEqual(job["state"], "pending")
        output = self.cli("run", "consumer", "--details", env=environment)
        self.finish()
        self.assertIn("consumer__join: confirmed submitted", output)
        for name in names:
            self.assertNotRegex(output, rf"{name}__\w+: confirmed submitted")
        self.assertEqual((self.work / "results/consumer/text.txt").read_text(), "hello\nhello\nhello\n")
        self.assert_runtime("consumer", "candidate")
        self.assertEqual(self.trace(), Counter({**{name: 1 for name in names}, "consumer": 1}))
        self.assertFalse(list((self.work / "work/cleaned").iterdir()))
        self.assertEqual((self.work / "workflow.py").read_bytes(), workflow)
        for path, expected in identities.items():
            self.assertEqual({"device": Path(path).stat().st_dev, "inode": Path(path).stat().st_ino}, expected)
        after = self.snapshot()
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertNoSubmissions(self.cli("run", env=environment))
        unowned = self.work / "results/unowned/text.txt"
        unowned.parent.mkdir()
        unowned.write_text("not managed")
        self.assertIn("existing files are not adopted", self.cli("run", "unowned", success=False,
                                                                env={**self.environment, "GWFLOW_COMPAT_UNOWNED": "1"}))
        self.assertEqual(unowned.read_text(), "not managed")
        self.assertEqual(self.trace(), Counter({**{name: 1 for name in names}, "consumer": 1}))
        self.record({"unchanged_workflow_sha256": hashlib.sha256(workflow).hexdigest(),
                     "same_storage": identities, "preserved_baseline_files": len(before),
                     "computation_counts": dict(self.trace()), "retained_consumer": "hello\nhello\nhello\n",
                     "unowned_results_preserved": True})

    def assert_runtime(self, task, side):
        runtime = json.loads((self.work / "results" / task / "runtime.json").read_text())
        self.assertEqual(runtime["prefix"], self.installations[side]["prefix"])
        self.assertTrue(Path(runtime["package"]).is_relative_to(runtime["prefix"]))
        self.assertEqual(runtime["python"], self.installations[side]["python_version"])
        for name, version in runtime["versions"].items():
            self.assertEqual(version, self.installations[side]["versions"][name])
            if self.installations[side]["dependency_policy"] == "independent":
                self.assertTrue(Path(runtime["imports"][name]).resolve().is_relative_to(runtime["prefix"]))
        self.record({"task": task, "runtime": runtime})


class IncompleteCompatibilityTests(InstalledCompatibilityTests):
    settle = TaskGraphTests.settle

    def setUp(self):
        self.history = {}
        super().setUp()
        self.environment["PYTHONPATH"] = str(self.work)
        self.configure(backend="recovery_fixture")
        self.inject()

    def inject(self, **options):
        return super().inject(**{"record_acceptances": True, "submission_states": self.history, **options})

    def accepted_submissions(self):
        return [json.loads(line) for line in (self.work / "backend-acceptances.jsonl").read_text().splitlines()]

    def handoff(self, *external_roots):
        self.settle()
        with Client.connect(get_socket_path(self.work)) as client:
            states = client.status()
        self.assertTrue(states and all(state in (LocalStatus.COMPLETED, LocalStatus.FAILED,
                                                LocalStatus.CANCELLED, LocalStatus.KILLED)
                                       for state in states.values()), states)
        accepted = self.accepted_submissions()
        self.history = {job["name"]: ("FAILED" if states[str(job["id"])] == LocalStatus.KILLED
                                     else states[str(job["id"])].name) for job in accepted}
        self.assertEqual({str(job["id"]) for job in accepted}, set(states))
        self.workflow_before = (self.work / "workflow.py").read_bytes()
        self.storage_before = {str(path): {"device": path.stat().st_dev, "inode": path.stat().st_ino}
                               for path in (self.work, self.work / "work", self.work / "results",
                                            self.work / ".gwf/gwflow", *external_roots) if path.exists()}
        before = self.snapshot()
        self.stop_worker(self.worker)
        self.assertIsNotNone(self.worker.poll())
        self.record({"all_jobs_inactive": True, "worker_exit": self.worker.returncode,
                     "job_states": {str(job): state.name for job, state in states.items()},
                     "accepted_submissions": accepted, "replayed_terminal_states": self.history,
                     "storage": self.storage_before, "snapshot": before})
        self.select_installation("candidate")
        self.environment["PYTHONPATH"] = str(self.work)
        self.inject()
        self.worker = self.start_worker()
        self.cli("status", "--details")
        self.assertEqual(self.snapshot(), before)
        self.assert_same_storage()

    def assert_same_storage(self):
        self.assertEqual((self.work / "workflow.py").read_bytes(), self.workflow_before)
        for path, expected in self.storage_before.items():
            self.assertEqual({"device": Path(path).stat().st_dev, "inode": Path(path).stat().st_ino}, expected)

    def assert_preserved(self, attempts, counts, retained):
        self.assertEqual(self.task_attempts(), attempts)
        observed_counts = Counter((self.work / "trace").read_text().splitlines())
        self.assertEqual(observed_counts, counts)
        observed_outputs = {str(path): path.read_text() for path in retained}
        self.assertEqual(observed_outputs, {str(path): content for path, content in retained.items()})
        self.assert_same_storage()
        self.assertNoSubmissions(self.cli("run"))
        self.record({"preserved_attempts": attempts, "computation_counts": dict(observed_counts),
                     "retained_outputs": observed_outputs, "same_storage": self.storage_before,
                     "unchanged_workflow_sha256": hashlib.sha256(self.workflow_before).hexdigest()})


class PartialCompatibilityTests(IncompleteCompatibilityTests):
    def configure_workflow(self, **options):
        TaskGraphTests.configure_workflow(self, **options)

    interrupt_computation = OperatorRecoveryTests.interrupt_computation

    def test_failed_branch_preserves_successful_sibling_across_upgrade(self):
        failing = self.work / "fail-right"
        failing.touch()
        self.configure_workflow(right_command=(
            f"echo right >> {shlex.quote(str(self.work / 'trace'))}; "
            f"if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt"))
        self.cli("run")
        self.settle()
        attempts = self.task_attempts()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 1, "right": 1})
        identities = self.submission_identities("sample")
        self.handoff()
        self.consume_handoff("partial", attempts["sample"], identities)
        failing.unlink()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task sample\s+Retry\s+")
        self.cli("run")
        self.finish()
        self.assert_preserved(attempts, {"left": 1, "right": 2, "join": 1},
                              {self.work / "results/sample/result.txt": "leftright"})

    def test_recorded_recovery_survives_upgrade_and_does_not_resolve_new_admissions(self):
        self.interrupt_computation()
        attempts = self.task_attempts()
        output = self.cli("recover", "sample", env=self.inject(job_states={"sample": "UNKNOWN"}))
        self.assertEqual(output.count("Resolved submission"), 3, output)
        records = self.logical_records("operator-recovery")
        self.assertEqual(len(records), 3)
        self.assertFalse((self.work / "results/sample").exists())
        self.handoff()
        self.history = dict.fromkeys(self.history, "UNKNOWN")
        self.inject()
        self.record({"expired_scheduler_history": self.history})
        self.assertIn("No unresolved submissions", self.cli("recover", "sample"))
        self.assertRegex(self.cli("run", "--dry-run"), r"Task sample\s+Retry\s+")
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["left"])
        self.assertFalse((self.work / "results/sample").exists())
        self.cli("run", env=self.inject(reject_prefix="sample__right"), success=False)
        self.inject()
        self.assertIn("unresolved submission", self.cli("run", success=False))
        self.assertEqual({name: self.logical_records("operator-recovery")[name] for name in records}, records)
        output = self.cli("recover", "sample")
        self.assertEqual(output.count("Resolved submission"), 1, output)
        self.assertEqual(len(self.logical_records("operator-recovery")), 4)
        self.cli("run")
        self.finish()
        self.assertEqual({name: self.logical_records("operator-recovery")[name] for name in records}, records)
        self.assert_preserved(attempts, {"left": 1, "right": 1, "join": 1},
                              {self.work / "results/sample/result.txt": "leftright"})
        self.record({"preserved_recovery_records": len(records), "new_recovery_records": 1})

    def test_upgrade_preserves_command_and_input_invalidation_after_recovery(self):
        workflow = self.work / "workflow.py"
        original = workflow.read_text().replace("empty_task(inputs=[])", "empty_task(inputs=['input.txt'])")
        workflow.write_text(original)
        self.interrupt_computation()
        attempts = self.task_attempts()
        self.cli("recover", "sample", env=self.inject(job_states={"sample": "UNKNOWN"}))
        self.handoff()
        before = self.snapshot()
        workflow.write_text(original.replace("printf right", "printf changed"))
        self.assertIn("changed commands", self.cli("run", "--dry-run", "--details"))
        self.assertEqual(self.snapshot(), before)
        workflow.write_text(original)
        (self.work / "input.txt").write_text("changed external input\n")
        self.assertIn("input metadata changed", self.cli("run", "--dry-run", "--details"))
        self.assertEqual(self.snapshot(), before)
        self.cli("run")
        self.finish()
        self.assertNotEqual(self.task_attempts()["sample"], attempts["sample"])
        counts = Counter((self.work / "trace").read_text().splitlines())
        self.assertEqual(counts, {"left": 2, "right": 1, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assert_same_storage()
        self.record({"changed_commands_require_fresh": True, "changed_inputs_require_fresh": True,
                     "old_attempts": attempts, "new_attempts": self.task_attempts(), "computation_counts": dict(counts)})


class LogicalInputCompatibilityTests(IncompleteCompatibilityTests):
    def configure_workflow(self):
        fixture = FIXTURES / "logical_input_compatibility"
        for name in ("workflow.py", "read_text.py"):
            shutil.copy(fixture / name, self.work / name)
        source = (self.work / "input.txt").read_bytes()
        (self.work / "input.txt.gz").write_bytes(gzip.compress(source, mtime=0))

    def observation(self):
        task, = json.loads(self.cli_result("status", "sample", "--json", "--details").stdout)["tasks"]
        retained = self.work / "results/sample/text.txt"
        observation = {
            "attempt": task["attempt_id"], "state": task["state"], "action": task["next_action"],
            "jobs": {job["name"]: {key: job[key] for key in ("state", "submission_name", "backend_id")}
                     for job in task["jobs"]},
            "executions": [json.loads(line) for line in (self.work / "executions.jsonl").read_text().splitlines()],
            "submissions": self.accepted_submissions(),
            "retained_text": retained.read_text() if retained.exists() else None,
            "selected_encoding": "gzip" if (self.work / "select-gzip").exists() else "plain",
            "available_sources": [name for name in ("input.txt", "input.txt.gz") if (self.work / name).exists()],
        }
        self.record({"logical_input_observation": observation})
        return observation

    def assert_executions(self, observation, expected):
        executions = observation["executions"]
        self.assertEqual(len(executions), len(expected))
        for observed, (step, side, encoding) in zip(executions, expected):
            source = "input.txt.gz" if encoding == "gzip" else "input.txt"
            self.assertEqual(observed, {"step": step, "prefix": self.installations[side]["prefix"],
                "descriptor": {"name": "text", "identity": "greeting-v1", "metadata": {"encoding": encoding},
                               "files": {"source": str(self.work / source)}}})

    def assert_new_submissions(self, before, after, jobs):
        old = before["submissions"]
        self.assertEqual(after["submissions"][:len(old)], old)
        expected = [after["jobs"]["sample__" + name] for name in jobs]
        self.assertCountEqual(after["submissions"][len(old):],
                              [{"name": job["submission_name"], "id": job["backend_id"]} for job in expected])

    def test_logical_input_cleaned_reuse_across_upgrade(self):
        self.run_complete()
        self.cli("clean", "sample", "--delete")
        work = self.work / "work/sample"
        self.assertFalse(any(work.iterdir()))
        before = self.observation()
        self.assertIsInstance(before["attempt"], str)
        self.assertEqual(before["retained_text"], "hello\nhello\n")
        self.assert_executions(before, [("decode", "baseline", "plain"), ("join", "baseline", "plain")])
        frozen = self.snapshot()
        (self.work / "select-gzip").touch()
        for name in ("input.txt", "input.txt.gz"):
            (self.work / name).unlink()
        self.handoff()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task sample\s+Reuse\s+")
        self.assertNoSubmissions(self.cli("run"))
        after = self.observation()
        self.assertEqual(after, {**before, "state": "reusable", "action": "reuse",
                                 "selected_encoding": "gzip", "available_sources": []})
        self.assertEqual(self.snapshot(), frozen)
        self.assertFalse(any(work.iterdir()))

    def partial_handoff(self):
        fail = self.work / "fail-join"
        fail.touch()
        self.cli("run")
        self.settle()
        before = self.observation()
        self.assertIsInstance(before["attempt"], str)
        self.assertEqual(before["action"], "retry")
        self.assertIsNone(before["retained_text"])
        self.assertEqual(before["jobs"]["sample__decode"]["state"], "completed")
        self.assertEqual(before["jobs"]["sample__join"]["state"], "failed")
        self.assert_executions(before, [("decode", "baseline", "plain"), ("join", "baseline", "plain")])
        self.handoff()
        fail.unlink()
        return before

    def test_logical_input_partial_retry_across_upgrade(self):
        before = self.partial_handoff()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task sample\s+Retry\s+")
        self.run_complete()
        after = self.observation()
        self.assertEqual(after["attempt"], before["attempt"])
        self.assertEqual(after["jobs"]["sample__decode"], before["jobs"]["sample__decode"])
        self.assert_new_submissions(before, after, ["join", "gwflow_complete"])
        self.assert_executions(after, [("decode", "baseline", "plain"), ("join", "baseline", "plain"),
                                       ("join", "candidate", "plain")])
        self.assertEqual((after["action"], after["retained_text"]), ("reuse", "hello\nhello\n"))

    def test_logical_input_changed_representation_restarts_partial_attempt(self):
        before = self.partial_handoff()
        (self.work / "select-gzip").touch()
        preview = self.cli("run", "--dry-run", "--details")
        self.assertIn("changed logical input representation", preview)
        self.assertIn("fresh attempt", preview)
        self.run_complete()
        after = self.observation()
        self.assertNotEqual(after["attempt"], before["attempt"])
        self.assert_new_submissions(before, after, ["gwflow_prepare", "decode", "join", "gwflow_complete"])
        self.assert_executions(after, [("decode", "baseline", "plain"), ("join", "baseline", "plain"),
                                       ("decode", "candidate", "gzip"), ("join", "candidate", "gzip")])
        self.assertEqual((after["action"], after["retained_text"]), ("reuse", "hello\nhello\n"))


class PreparationCompatibilityTests(IncompleteCompatibilityTests):
    fault = ExternalInputTests.fault

    def configure_workflow(self):
        ExternalInputTests.configure_workflow(self, command=(
            "printf '%s\\n' {source} > alias.txt; cat {source} > {out}; "
            f"echo read >> {shlex.quote(str(self.work / 'trace'))}"))

    def preparation_interruption(self, phase):
        self.cli("run", env=self.fault(**{phase: True}))
        self.settle()
        attempts = self.task_attempts()
        self.assertFalse((self.work / "trace").exists())
        inputs = self.logical_records("inputs")
        self.assertEqual(len(inputs), 1 if phase == "crash_after_baseline" else 0)
        self.handoff()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task sample\s+Retry\s+")
        self.cli("run")
        self.finish()
        self.assertEqual({name: self.logical_records("inputs")[name] for name in inputs}, inputs)
        self.assert_preserved(attempts, {"read": 1}, {self.work / "results/sample/result.txt": "hello\n",
                              self.work / "results/sample/alias.txt": str(self.work / "input.txt") + "\n"})

    def test_interrupted_preparation_before_input_baseline_survives_upgrade(self):
        self.preparation_interruption("crash_before_baseline")

    def test_interrupted_preparation_preserves_accepted_input_baseline_across_upgrade(self):
        self.preparation_interruption("crash_after_baseline")


class FinishingCompatibilityTests(IncompleteCompatibilityTests):
    def configure_workflow(self, **options):
        TransferRecoveryTests.configure_workflow(self, **options)

    assert_results = TransferRecoveryTests.assert_results
    fault = TransferRecoveryTests.fault
    separate_filesystem = TransferRecoveryTests.separate_filesystem
    add_consumer = TransferRecoveryTests.add_consumer
    finishing_generation = ResultsRepairTests.finishing_generation

    def finishing_interruption(self, phase):
        self.cli("run", env=self.fault(**{phase: True}))
        self.settle()
        attempts = self.task_attempts()
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        result = self.work / "results/samples/a/report"
        installed = phase == "crash_after_results_install"
        self.assertEqual(result.exists(), installed)
        self.handoff(*([result] if installed else []))
        preview = self.cli("run", "--dry-run")
        self.assertRegex(preview, r"Task a\s+Finish\s+")
        if phase == "crash_after_manifest":
            self.assertIn("install prepared", preview)
        if installed:
            self.assertIn("finish checked installed", preview)
        self.cli("run")
        self.finish()
        self.assert_results(result)
        self.assert_preserved(attempts, {"compute": 1}, {result / "renamed.txt": "first",
                                                       result / "nested/two.txt": "second"})

    def test_partial_copy_survives_upgrade_without_computation(self):
        self.finishing_interruption("crash_during_copy")

    def test_prepared_installation_survives_upgrade_without_computation(self):
        self.finishing_interruption("crash_after_manifest")

    def test_installed_results_finish_completion_after_upgrade(self):
        self.finishing_interruption("crash_after_results_install")

    def test_interrupted_repair_preserves_computation_and_consumer_across_upgrade(self):
        self.add_consumer()
        self.run_complete()
        attempts = self.task_attempts()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").write_text("damaged retained set")
        self.cli("run", env=self.fault(crash_during_copy=True))
        self.settle()
        generation = self.finishing_generation()
        self.assertEqual((result / "renamed.txt").read_text(), "damaged retained set")
        self.handoff()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task c\s+Defer\s+")
        self.cli("run")
        self.finish()
        self.assertEqual(self.finishing_generation(), generation)
        self.assert_results(result)
        self.assert_preserved(attempts, {"compute": 1, "consumer": 1},
                              {result / "renamed.txt": "first", result / "nested/two.txt": "second",
                               self.work / "results/c/joined.txt": "firstsecond"})

    def test_partial_copy_on_separate_filesystem_survives_upgrade(self):
        remote = self.separate_filesystem()
        self.configure_workflow(settings=f"results_root={str(remote / 'results')!r}, results_staging_root={str(remote / 'staging')!r}")
        self.cli("run", env=self.fault(crash_during_copy=True))
        self.settle()
        attempts = self.task_attempts()
        self.handoff(remote, remote / "results", remote / "staging")
        self.assertNotEqual(remote.stat().st_dev, self.work.stat().st_dev)
        self.assertRegex(self.cli("run", "--dry-run"), r"Task a\s+Finish\s+")
        self.cli("run")
        self.finish()
        result = remote / "results/samples/a/report"
        self.assert_results(result)
        self.assert_preserved(attempts, {"compute": 1}, {result / "renamed.txt": "first",
                                                       result / "nested/two.txt": "second"})

    def test_missing_retained_output_remains_repairable_after_upgrade(self):
        self.run_complete()
        attempts = self.task_attempts()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").unlink()
        self.handoff()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task a\s+Repair\s+")
        self.cli("run")
        self.finish()
        self.assert_results(result)
        self.assert_preserved(attempts, {"compute": 1}, {result / "renamed.txt": "first",
                                                       result / "nested/two.txt": "second"})

    def test_missing_source_evidence_still_requires_fresh_attempt_after_upgrade(self):
        self.run_complete()
        attempts = self.task_attempts()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").unlink()
        state_path = next((self.work / ".gwf/gwflow").rglob("record-state.json"))
        state = json.loads(state_path.read_text())
        name = next(name for name in state["records"] if name.endswith("/success.json"))
        del state["records"][name]
        state_path.write_text(json.dumps(state))
        self.handoff()
        self.assertRegex(self.cli("run", "--dry-run"), r"Task a\s+Run\s+")
        self.cli("run")
        self.finish()
        self.assertNotEqual(self.task_attempts()["a"], attempts["a"])
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute", "compute"])
        self.assert_results(result)
        self.assert_same_storage()
        self.record({"missing_source_evidence_requires_fresh": True, "old_attempts": attempts,
                     "new_attempts": self.task_attempts(), "computation_counts": {"compute": 2}})

    def test_uncertain_and_active_consumers_still_block_repair_after_upgrade(self):
        self.run_complete()
        self.add_consumer()
        self.cli("run", env=self.inject(reject_prefix="c__gwflow_prepare"), success=False)
        result = self.work / "results/samples/a/report/renamed.txt"
        result.write_text("protected retained output")
        self.handoff()
        accepted = (self.work / "backend-acceptances.jsonl").read_bytes()
        for options in ({}, {"running_prefix": "c"}):
            output = self.cli("run", "a", env=self.inject(**options), success=False)
            self.assertIn("Active consumers block replacement: c", output)
            self.assertEqual(result.read_text(), "protected retained output")
            self.assertEqual((self.work / "backend-acceptances.jsonl").read_bytes(), accepted)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.assert_same_storage()
        self.record({"uncertain_consumer_blocks_repair": True, "active_consumer_blocks_repair": True,
                     "protected_output": result.read_text(), "new_submissions": 0})

    def test_insufficient_result_ownership_still_blocks_repair_after_upgrade(self):
        self.run_complete()
        result = self.work / "results/samples/a/report"
        (result / "renamed.txt").unlink()
        self.cli("run", env=self.inject(reject_before_admission=True), success=False)
        result.rename(result.with_name("original"))
        result.mkdir()
        unowned = result / "unrelated.txt"
        unowned.write_text("unowned content")
        self.handoff()
        accepted = (self.work / "backend-acceptances.jsonl").read_bytes()
        self.assertIn("ownership", self.cli("run", success=False))
        self.assertEqual(unowned.read_text(), "unowned content")
        self.assertEqual((self.work / "backend-acceptances.jsonl").read_bytes(), accepted)
        self.assertEqual((self.work / "trace").read_text().splitlines(), ["compute"])
        self.assert_same_storage()
        self.record({"insufficient_ownership_blocks_repair": True, "unowned_output": unowned.read_text(),
                     "new_submissions": 0})


class CompatibilityResult(unittest.TextTestResult):
    def startTestRun(self):
        super().startTestRun()
        self.logical_inputs = []

    def addSuccess(self, test):
        super().addSuccess(test)
        if isinstance(test, LogicalInputCompatibilityTests):
            self.logical_inputs.append(test._testMethodName)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", type=Path, required=True)
    parser.add_argument("--candidate-python", type=Path, required=True)
    parser.add_argument("--baseline-source", required=True, help="Exact baseline source commit or artifact identifier")
    parser.add_argument("--candidate-source", required=True, help="Exact candidate source commit or artifact identifier")
    parser.add_argument("--root", type=Path, required=True, help="New directory for persistent storage and raw evidence")
    parser.add_argument("--test", action="append", help="Run only matching test names (glob; repeatable)")
    parser.add_argument("--independent", action="store_true", help="Require entirely separate installed runtimes")
    args = parser.parse_args()
    try:
        installations = {side: installed_environment(getattr(args, side + "_python"),
                                                     getattr(args, side + "_source"), args.independent)
                         for side in ("baseline", "candidate")}
        disjoint_environments(installations["baseline"], installations["candidate"])
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "environment.json").write_text(json.dumps({"comparison_kind": "independent-runtime" if args.independent else "provisional-source",
                                                      **installations}, indent=2) + "\n")
    loader = unittest.TestLoader()
    loader.testNamePatterns = args.test
    suite = unittest.TestSuite()
    consumer_reports = []
    cases = [CompletedCompatibilityTests, PartialCompatibilityTests, PreparationCompatibilityTests,
             FinishingCompatibilityTests]
    baseline_version = installations["baseline"]["versions"]["gwflow"]
    logical_inputs_applicable = args.independent and Version(baseline_version) >= Version("0.12.0")
    if logical_inputs_applicable:
        cases.append(LogicalInputCompatibilityTests)
    for base in cases:
        case = type(base.__name__, (base,), {"root": root, "installations": installations,
                                            "consumer_reports": consumer_reports})
        suite.addTests(loader.loadTestsFromTestCase(case))
    if not suite.countTestCases():
        parser.error("No compatibility tests matched")
    result = unittest.TextTestRunner(verbosity=2, resultclass=CompatibilityResult).run(suite)
    (root / "summary.json").write_text(json.dumps({"run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skips": len(result.skipped)}, indent=2) + "\n")
    (root / "coverage.json").write_text(json.dumps({
        "baseline_version": baseline_version, "independent": args.independent,
        "logical_inputs_applicable": logical_inputs_applicable,
        "logical_inputs": sorted(result.logical_inputs),
    }, indent=2) + "\n")
    complete = (set(consumer_reports) == CONSUMER_REPORTS and len(consumer_reports) == len(CONSUMER_REPORTS)
                and result.wasSuccessful() and not result.skipped)
    snapshot = json.loads((FIXTURES / "status_consumer_v1.json").read_text())
    actual_digest = hashlib.sha256((FIXTURES / "status_consumer_v1.py").read_bytes()).hexdigest()
    intact = actual_digest == snapshot["sha256"]
    consumer_evidence = {"snapshot": snapshot, "actual_sha256": actual_digest,
                         "reports": sorted(consumer_reports), "expected_reports": sorted(CONSUMER_REPORTS),
                         "status": "passed" if complete and intact and not args.test else "incomplete",
                         "selection": args.test}
    (root / "status-consumer.json").write_text(json.dumps(consumer_evidence, indent=2) + "\n")
    if not intact or (not args.test and not complete):
        print("Reference status consumer incomplete or snapshot changed", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    sys.exit(main())
