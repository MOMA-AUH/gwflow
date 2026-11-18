"""Exact Submission identity through installed CLI and scheduler commands."""

import json
import re
import shlex

from support import LocalBackendTestCase
import test_graphs


class SlurmIdentityTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    slurm_environment = test_graphs.TaskGraphTests.slurm_environment

    def held_submission(self, *, alias="sample", extra_task=False):
        held, release = self.work / "held", self.work / "release"
        self.configure_workflow(right_command=(
            f"touch {shlex.quote(str(held))}; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf right > same.txt"))
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("alias='sample'", f"alias={alias!r}"))
        if extra_task:
            with workflow.open("a") as stream:
                stream.write("other = empty_task(inputs=[])\n"
                             "other_job = other.target('work', inputs=[], outputs=['out'])\n"
                             "other_job << 'touch out'\n"
                             "other.retain('out', source=other_job.output('out'), path='out')\n"
                             "gwf.task(other, alias='other')\n")
        self.addCleanup(release.touch)
        self.environment = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=self.environment)
        self.wait_for(held.exists)
        return next(item for item in self.submissions() if item["name"].startswith(alias + "__right__"))

    def submissions(self):
        return list(map(json.loads, (self.work / "slurm-submitted.jsonl").read_text().splitlines()))

    def overrides(self, job, name, state="RUNNING"):
        (self.work / "slurm-observations.json").write_text(json.dumps({
            command: {job["id"]: {"name": name, "state": state}}
            for command in ("squeue", "sacct", "scancel")}))

    def requests(self):
        path = self.work / "slurm-cancelled.jsonl"
        return list(map(json.loads, path.read_text().splitlines())) if path.exists() else []

    def assert_recovery_veto(self, env, *jobs):
        receipts = {path: path.read_bytes() for path in self.work.rglob("recovery.json")}
        output = self.cli("-b", "slurm", "recover", "sample", env=env, success=False)
        self.assertIn("queued or running submissions", output)
        for job in jobs:
            self.assertIn(job["name"], output)
        self.assertEqual({path: path.read_bytes() for path in self.work.rglob("recovery.json")}, receipts)
        preview = self.cli("-b", "slurm", "run", "--dry-run", "--details", env=env, success=False)
        self.assertNotIn("Would submit", preview)

    def test_live_computation_and_completion_veto_recovery_when_accounting_fails(self):
        self.environment = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=self.environment)
        running = next(job for job in self.submissions() if "__right__" in job["name"])
        pending = next(job for job in self.submissions() if "__gwflow_complete__" in job["name"])
        (self.work / "slurm-observations.json").write_text(json.dumps({
            "squeue": {running["id"]: {"state": "RUNNING"}, pending["id"]: {"state": "PENDING"}}}))
        (self.work / "sacct-error").touch()
        self.assert_recovery_veto(self.environment, running, pending)
        (self.work / "sacct-error").unlink()
        (self.work / "slurm-observations.json").write_text(json.dumps({
            "squeue": {running["id"]: {"state": "RUNNING", "extra_rows": ["bad-id|R|other"]},
                       pending["id"]: {"state": "PENDING"}}}))
        self.assert_recovery_veto(self.environment, running, pending)

    def test_live_computation_vetoes_recovery_after_lost_acknowledgement(self):
        release = self.work / "release"
        self.addCleanup(release.touch)
        self.configure_workflow(right_command=(
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf right > same.txt"))
        env = self.slurm_environment()
        output = self.cli("-b", "slurm", "run", success=False,
                          env={**env, "GWFLOW_TEST_SLURM_LOSE_ACK": "sample__right"})
        self.assertIn("lost Slurm acknowledgement", output)
        job = next(job for job in self.submissions() if "__right__" in job["name"])
        self.overrides(job, job["name"], "RUNNING")
        self.assert_recovery_veto(env, job)
        # An earlier operator assertion cannot suppress a reappearing Submission.
        forgotten = self.work / "forgotten-slurm-history"
        forgotten.touch()
        self.assertIn(job["name"], self.cli("-b", "slurm", "recover", "sample", env=env))
        forgotten.unlink()
        self.assert_recovery_veto(env, job)

    def test_recycled_running_id_is_unresolved_and_never_cancelled(self):
        job = self.held_submission()
        self.overrides(job, "unrelated-same-user-job")
        output = self.cli("-b", "slurm", "cancel", "sample", "--details", env=self.environment, success=False)
        self.assertIn("Slurm Submission identity", output)
        requests = list(map(json.loads, (self.work / "slurm-cancelled.jsonl").read_text().splitlines()))
        self.assertNotIn(job["id"], [item["id"] for item in requests])
        self.assertTrue(requests, "independently identified queued siblings must still be requested")
        self.assertIn("unresolved submission", self.cli("-b", "slurm", "run", "--force", env=self.environment, success=False))
        status = self.cli("-b", "slurm", "status", "sample", "--json", env=self.environment)
        self.assertIn('"blocked"', status)
        self.assertIn('"submission_unresolved"', status)
        waiting = self.cli_result("-b", "slurm", "wait", "sample", "--timeout", "10",
                                  env=self.environment, success=False)
        self.assertEqual(waiting.returncode, 124)
        self.assertIn("unresolved", waiting.stdout + waiting.stderr)

    def test_controller_rechecks_full_name_after_observation(self):
        job = self.held_submission()
        (self.work / "slurm-observations.json").write_text(json.dumps({
            "scancel": {job["id"]: {"name": "replacement-after-observation"}}}))
        result = self.cli_result("-b", "slurm", "cancel", "sample", "--details", env=self.environment, success=False)
        self.assertIn("No active jobs match ALL job filters", result.stdout + result.stderr)
        self.assertNotIn(job["id"], [item["id"] for item in self.requests()])
        self.assertTrue(self.requests())
        for request in self.requests():
            self.assertIn("--ctld", request["args"])
            self.assertIn("--name=" + request["name"], request["args"])

    def test_numeric_terminal_cache_does_not_hide_foreign_terminal_jobs(self):
        job = self.held_submission()
        cache = self.work / ".gwf/slurm-backend-status-cache.json"
        for state, value in (("COMPLETED", 3), ("FAILED", 4), ("CANCELLED", 5)):
            with self.subTest(state=state):
                self.overrides(job, "foreign-terminal-job", state)
                cache.write_text(json.dumps({job["id"]: value}))
                output = self.cli("-b", "slurm", "cancel", "sample", "--dry-run", env=self.environment, success=False)
                self.assertIn("Slurm Submission identity", output)
                self.assertFalse(self.requests())
                output = self.cli("-b", "slurm", "run", "--force", env=self.environment, success=False)
                self.assertIn("unresolved submission", output)
                attempt = re.search(r"attempt ([0-9a-f]{32})", self.cli(
                    "-b", "slurm", "logs", "sample", "--details", env=self.environment)).group(1)
                self.assertIn("Cleanup refused", self.cli("-b", "slurm", "clean", "--attempt", attempt,
                              "--delete", env=self.environment, success=False))

    def test_foreign_job_does_not_override_checked_completion(self):
        self.environment = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=self.environment)
        self.finish()
        result = self.work / "results/sample/result.txt"
        before = result.read_bytes()
        job = self.submissions()[-1]
        for state in ("RUNNING", "PENDING", "COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(state=state):
                self.overrides(job, "unrelated-job", state)
                self.assertIn("Reuse", self.cli("-b", "slurm", "run", "--dry-run", env=self.environment))
                self.cli("-b", "slurm", "wait", "sample", "--timeout", "10", env=self.environment)
                self.cli("-b", "slurm", "cancel", "sample", env=self.environment)
                self.assertFalse(self.requests())
                self.assertEqual(result.read_bytes(), before)

    def test_contradictory_truncated_and_missing_names_refuse_cancellation(self):
        job = self.held_submission()
        for name in (job["name"][:-1], "sample__right", ""):
            with self.subTest(name=name):
                # A matching live record must not override contradictory accounting identity.
                (self.work / "slurm-observations.json").write_text(json.dumps({
                    "sacct": {job["id"]: {"name": name}}}))
                self.cli("-b", "slurm", "cancel", "sample", "--dry-run", env=self.environment, success=False)
                self.assertFalse(self.requests())

    def test_missing_history_and_query_failure_remain_unresolved(self):
        self.held_submission()
        for marker in ("forgotten-slurm-history", "slurm-query-error"):
            with self.subTest(marker=marker):
                path = self.work / marker
                path.touch()
                output = self.cli("-b", "slurm", "cancel", "sample", env=self.environment, success=False)
                self.assertIn("Slurm Submission identity", output)
                self.assertFalse(self.requests())
                path.unlink()

    def test_unsupported_cancellation_client_refuses_without_requests(self):
        self.held_submission()
        output = self.cli("-b", "slurm", "cancel", "sample", success=False,
                          env={**self.environment, "GWFLOW_TEST_SCANCEL_VERSION": "slurm 23.11.10"})
        self.assertIn("requires scancel 24.11 or newer", output)
        self.assertFalse(self.requests())

    def test_saved_acknowledgements_without_tracking_are_batched(self):
        self.held_submission()
        (self.work / ".gwf/slurm-backend-tracked.json").write_text("{}")
        self.environment["GWFLOW_TEST_RECORD_SLURM_QUERIES"] = "1"
        self.cli("-b", "slurm", "cancel", "sample", "--details", env=self.environment)
        self.assertEqual(len(self.requests()), 3)
        queries = list(map(json.loads, (self.work / "slurm-queries.jsonl").read_text().splitlines()))
        self.assertLessEqual(len(queries), 2, queries)

    def test_full_long_names_and_selected_removed_tasks_keep_scope(self):
        alias = "long_task_" + "x" * 100
        job = self.held_submission(alias=alias, extra_task=True)
        unrelated = self.work / "results/other/out"
        self.wait_for(unrelated.exists)
        unrelated_before = (unrelated.read_bytes(), unrelated.stat().st_mtime_ns)
        self.assertGreater(len(job["name"]), 128)
        preview = self.cli("-b", "slurm", "cancel", alias, "--dry-run", "--details", env=self.environment)
        self.assertIn(job["name"], preview)
        self.assertFalse(self.requests())
        self.cli("-b", "slurm", "cancel", alias, env=self.environment)
        self.assertTrue(self.requests())
        self.assertTrue(all(item["name"].startswith(alias + "__") for item in self.requests()))
        for request in self.requests():
            self.assertIn("--name=" + request["name"], request["args"])
        (self.work / "slurm-cancelled.jsonl").unlink()
        # Removing declarations does not remove recorded current attempts.
        (self.work / "workflow.py").write_text("from gwflow import Workflow\ngwf = Workflow()\n")
        self.cli("-b", "slurm", "cancel", "*", env=self.environment)
        self.assertIn(job["name"], [item["name"] for item in self.requests()])
        self.assertTrue(all(item["name"] in {item["name"] for item in self.submissions()}
                            for item in self.requests()))
        self.assertEqual((unrelated.read_bytes(), unrelated.stat().st_mtime_ns), unrelated_before)

    def test_earlier_admission_of_current_attempt_is_still_identified(self):
        failing = self.work / "fail-right"
        failing.touch()
        self.configure_workflow(right_command=(
            f"if test -e {shlex.quote(str(failing))}; then exit 7; fi; printf right > same.txt"))
        self.environment = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=self.environment)
        test_graphs.TaskGraphTests.settle(self)
        old = next(item for item in self.submissions() if item["name"].startswith("sample__right__"))
        failing.unlink()
        self.cli("-b", "slurm", "run", env=self.environment)
        test_graphs.TaskGraphTests.settle(self)
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.overrides(old, old["name"])
        recovery = self.cli("-b", "slurm", "recover", "sample", env=self.environment, success=False)
        self.assertIn(old["name"], recovery)
        self.assertFalse(list(self.work.rglob("recovery.json")))
        self.cli("-b", "slurm", "cancel", "sample", env=self.environment)
        self.assertEqual([item["name"] for item in self.requests()], [old["name"]])
        (self.work / "slurm-cancelled.jsonl").unlink()
        self.overrides(old, "unrelated-replacement")
        self.cli("-b", "slurm", "cancel", "*", env=self.environment, success=False)
        self.assertFalse(self.requests())

    def test_incomplete_scheduler_row_does_not_hide_identifiable_siblings(self):
        job = self.held_submission()
        (self.work / "slurm-observations.json").write_text(json.dumps({
            "squeue": {job["id"]: {"reported_state": "   "}}}))
        output = self.cli("-b", "slurm", "cancel", "sample", "--details", env=self.environment, success=False)
        self.assertIn("Slurm Submission identity", output)
        self.assertNotIn(job["id"], [item["id"] for item in self.requests()])
        self.assertEqual(len(self.requests()), 2)

    def test_finishing_job_is_active_and_unrelated_arrays_do_not_block_it(self):
        job = self.held_submission()
        (self.work / "slurm-observations.json").write_text(json.dumps({
            "squeue": {job["id"]: {"state": "COMPLETING", "extra_rows": ["900_[1-20]|PD|other-array"]}}}))
        output = self.cli("-b", "slurm", "cancel", "sample", "--dry-run", env=self.environment)
        self.assertIn("Would request cancellation", output)
        self.assertIn("active work", self.cli("-b", "slurm", "run", "--force", env=self.environment, success=False))
        self.assertFalse(self.requests())

    def test_unparseable_live_identity_cannot_leave_terminal_accounting_authoritative(self):
        job = self.held_submission()
        (self.work / "slurm-observations.json").write_text(json.dumps({
            "squeue": {job["id"]: {"reported_id": "unreadable-id"}},
            "sacct": {job["id"]: {"state": "COMPLETED"}}}))
        output = self.cli("-b", "slurm", "cancel", "sample", "--dry-run", env=self.environment, success=False)
        self.assertIn("Slurm Submission identity", output)
        self.assertFalse(self.requests())

    def test_truncated_unrelated_array_ids_do_not_break_status(self):
        job = self.held_submission()
        for array_id in ("900_[185,188-191,194-195,19", "900_[25,33,42-43,45-46,48,5",
                         "900_[334-335,338-339,342-34"):
            with self.subTest(array_id=array_id):
                (self.work / "slurm-observations.json").write_text(json.dumps({
                    "squeue": {job["id"]: {"extra_rows": [f"{array_id}|PD|other-array"]}}}))
                result = self.cli_result("-b", "slurm", "status", "sample", "--json", env=self.environment)
                status = json.loads(result.stdout)
                self.assertEqual(status["tasks"][0]["state"], "running")
                output = self.cli("-b", "slurm", "cancel", "sample", "--dry-run", env=self.environment)
                self.assertIn("Would request cancellation", output)
                self.assertFalse(self.requests())

    def test_recovered_earlier_admissions_without_tracking_share_accounting_batch(self):
        failing = self.work / "fail-right"
        failing.touch()
        self.configure_workflow(right_command=(
            f"if test -e {shlex.quote(str(failing))}; then exit 7; fi; printf right > same.txt"))
        self.environment = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=self.environment)
        test_graphs.TaskGraphTests.settle(self)
        # Computation stopped independently; lost accounting requires the
        # ordinary operator assertion before retrying these admissions.
        forgotten = self.work / "forgotten-slurm-history"
        forgotten.touch()
        self.cli("-b", "slurm", "recover", "sample", env=self.environment)
        forgotten.unlink()
        failing.unlink()
        self.cli("-b", "slurm", "run", env=self.environment)
        test_graphs.TaskGraphTests.settle(self)
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        (self.work / ".gwf/slurm-backend-tracked.json").write_text("{}")
        self.environment["GWFLOW_TEST_RECORD_SLURM_QUERIES"] = "1"
        self.assertIn("Reuse", self.cli("-b", "slurm", "run", "--dry-run", env=self.environment))
        queries = list(map(json.loads, (self.work / "slurm-queries.jsonl").read_text().splitlines()))
        self.assertLessEqual(len(queries), 3, queries)
