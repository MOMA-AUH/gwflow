"""Managed cancellation through the installed CLI and real backend boundaries."""

from collections import Counter
import json
import re
import shlex
import shutil

from support import FIXTURES, LocalBackendTestCase
from terminal_session import rendered_lines
import test_graphs
import test_inspection
import test_run_selection


class CancellationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    slurm_environment = test_graphs.TaskGraphTests.slurm_environment
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def test_four_completed_jobs_share_one_outcome_without_repeated_identities(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().split("join = task.target")[0] +
                            "task.retain('right', source=right.output('same.txt'), path='result.txt')\n"
                            "gwf.task(task, alias='sample')\n")
        self.run_complete()
        before = self.snapshot()
        result = self.cli_result("cancel", "sample")
        self.assertIn("No admitted work to cancel", result.stdout)
        self.assertIn("All 4 jobs confirmed inactive (completed)", result.stdout)
        self.assertNotIn("Submission:", result.stdout)
        self.assertNotIn("Attempt:", result.stdout)
        self.assertNotIn("sample__", result.stdout)
        self.assertNotIn("confirmed inactive", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_inactive_jobs_are_distinguished_from_unavailable_live_state(self):
        self.run_complete()
        before = self.snapshot()
        output = self.cli("cancel", "sample")
        self.assertIn("All 5 jobs confirmed inactive (completed)", output)
        self.assertIn("No admitted work to cancel", output)
        environment = self.inject(job_states={"sample": "UNKNOWN"}, record_cancellations=True)
        output = self.cli("-b", "recovery_fixture", "cancel", "sample", env=environment)
        self.assertNotIn("confirmed inactive", output)
        self.assertEqual(output.count("Recorded successful work; live state unavailable."), 5, output)
        self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
        self.assertEqual(self.snapshot(), before)

    def test_details_keep_every_identity_and_mixed_outcomes_lead_with_counts(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
        submissions = re.findall(r"; submission (\S+)", index)
        backend_ids = re.findall(r"Backend job: (\S+)", index)
        self.assertEqual(len(submissions), 5)
        self.assertEqual(len(backend_ids), 5)
        environment = self.inject(job_states={"sample__right": "RUNNING", "sample__join": "UNKNOWN"},
                                  record_cancellations=True, accept_cancel_only=True)
        before = self.snapshot()
        result = self.cli_result("-b", "recovery_fixture", "cancel", "sample", "--details", env=environment)
        self.assertIn("3 jobs confirmed inactive (completed)", result.stdout)
        self.assertIn("1 cancellation request accepted; jobs may still be stopping", result.stdout)
        self.assertIn("Recorded successful work; live state unavailable.", result.stdout)
        self.assertLess(result.stdout.index("3 jobs confirmed inactive"), result.stdout.index("sample__gwflow_prepare:"))
        self.assertIn(attempt, result.stdout)
        for submission in submissions:
            self.assertIn(submission, result.stdout)
        for job_id in backend_ids:
            self.assertIn("Backend job: " + job_id, result.stdout)
        requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
        self.assertEqual(requested, [next(name for name in submissions if name.startswith("sample__right__"))])
        self.assertNotIn("Cancellation requested", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_interruption_retains_accepted_and_unconfirmed_requests_without_retrying(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        left = re.search(r"sample__left: complete; submission (\S+)", index).group(1)
        before = self.snapshot()
        environment = self.inject(job_states={"sample": "RUNNING"}, record_cancellations=True,
                                  accept_cancel_only=True, interrupt_cancel_prefix="sample__left")
        result = self.cli_result("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
        self.assertIn("Cancellation interrupted", result.stdout)
        self.assertIn("1 cancellation request accepted; jobs may still be stopping", result.stdout)
        self.assertIn("1 request outcome unconfirmed", result.stdout)
        self.assertIn("3 jobs not processed", result.stdout)
        self.assertIn("Already accepted requests were not rolled back", result.stdout)
        self.assertIn(left, result.stdout)
        self.assertNotIn("No admitted work to cancel", result.stdout)
        self.assertIn("Aborted", result.stderr)
        requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
        self.assertEqual(len(requested), 2)
        self.assertEqual(requested[1], left)
        self.assertEqual(self.snapshot(), before)

    def test_terminal_plain_and_redirected_receipts_preserve_failures_and_complete_identities(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        left = re.search(r"sample__left: complete; submission (\S+)", index).group(1)
        environment = self.inject(job_states={"sample__left": "RUNNING", "sample__right": "RUNNING"},
                                  reject_cancel_prefix="sample__left", accept_cancel_only=True,
                                  record_cancellations=True)
        before = self.snapshot()
        for mode in ("terminal", "plain", "no_color", "redirected"):
            with self.subTest(mode=mode):
                args = ("-b", "recovery_fixture", "cancel", "sample")
                if mode == "plain":
                    args += ("--plain",)
                if mode == "redirected":
                    result = self.cli_result(*args, env=environment, success=False)
                    visible = result.stdout
                else:
                    result = self.terminal_cli(*args, width=180 if mode == "terminal" else 32,
                                               no_color=mode == "no_color", merge_stderr=True,
                                               env=environment, success=False)
                    visible = rendered_lines(result.stdout) if mode == "terminal" else result.stdout
                self.assertIn(left, visible)
                compact = " ".join(visible.split())
                self.assertIn("1 failed cancellation request", compact)
                self.assertIn("1 cancellation request accepted", compact)
                self.assertIn("Cancellation request failed: injected cancellation rejection", compact)
                self.assertIn("sample__right: Cancellation requested", compact)
                if mode in ("plain", "redirected"):
                    self.assertNotIn("\x1b[", result.stdout + result.stderr)
                if mode == "no_color":
                    self.assertNotRegex(result.stdout, r"\x1b\[[0-9;]*m")
                requests = self.work / "cancellation-requests.jsonl"
                self.assertEqual(len(requests.read_text().splitlines()), 2)
                requests.unlink()
                self.assertEqual(self.snapshot(), before)

    def test_backend_close_failure_retains_accepted_receipts_from_the_same_invocation(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        submissions = re.findall(r"; submission (\S+)", index)
        environment = self.inject(job_states={"sample": "RUNNING"}, record_cancellations=True,
                                  accept_cancel_only=True, fail_cancel_flush=True)
        before = self.snapshot()
        result = self.cli_result("-b", "recovery_fixture", "cancel", "sample",
                                 env=environment, success=False)
        self.assertIn("5 cancellation requests accepted; jobs may still be stopping", result.stdout)
        for submission in submissions:
            self.assertIn(submission, result.stdout)
        self.assertIn("injected cancellation tracking flush failure", result.stderr)
        self.assertIn("Traceback:", result.stderr)
        self.assertEqual(len((self.work / "cancellation-requests.jsonl").read_text().splitlines()), 5)
        self.assertEqual(self.snapshot(), before)

    def test_observation_failure_retains_saved_identity_without_requesting_that_job(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        left = re.search(r"sample__left: complete; submission (\S+)", index).group(1)
        job_id = re.search(r"Backend job: (\S+)", index.split("sample__left:", 1)[1]).group(1)
        environment = self.inject(job_states={"sample": "RUNNING"}, record_cancellations=True,
                                  accept_cancel_only=True, fail_tracking_prefix="sample__left")
        before = self.snapshot()
        result = self.cli_result("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
        self.assertIn("1 unresolved job identity; no request", result.stdout)
        self.assertIn("4 cancellation requests accepted", result.stdout)
        self.assertIn(left, result.stdout)
        self.assertIn("Backend job: " + job_id, result.stdout)
        self.assertIn("injected tracking lookup failure", result.stderr)
        requests = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
        self.assertEqual(len(requests), 4)
        self.assertNotIn(left, requests)
        self.assertEqual(self.snapshot(), before)

    def test_unresolved_admission_retains_identity_and_blocks_replacement_and_cleanup(self):
        held, release = self.work / "held", self.work / "release"
        self.configure_workflow(right_command=(
            f"touch {shlex.quote(str(held))}; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf right > same.txt"))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(lose_tracking="sample__right"), success=False)
        self.wait_for(held.exists)
        try:
            index = self.cli("logs", "sample", "--details")
            attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
            self.assertIn("sample__right: uncertain; submission ", index)
            submission = re.search(r"sample__right: uncertain; submission ([^;\s]+)", index).group(1)
            before = self.snapshot()
            environment = self.inject(record_cancellations=True, accept_cancel_only=True)
            result = self.cli_result("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
            self.assertIn("1 unresolved job identity; no request", result.stdout)
            self.assertIn("no recorded or tracked backend job ID", result.stdout)
            self.assertIn(submission, result.stdout)
            self.assertIn(attempt, result.stdout)
            self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
            self.assertIn("unresolved submission", self.cli("run", "sample", "--force", success=False))
            self.assertIn("Cleanup refused", self.cli("clean", "--attempt", attempt, "--delete", success=False))
            self.assertEqual(self.snapshot(), before)
        finally:
            release.touch()
        self.finish()

    def test_cancelled_completion_recovers_without_repeating_computation(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_after_manifest": True}))
        environment = self.inject(job_fault="sample__gwflow_complete", shell_epilogue=True)
        self.cli("-b", "recovery_fixture", "run", env=environment)
        self.wait_for((self.work / "manifest-held").exists)
        try:
            index = self.cli("logs", "sample", "--details")
            attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
            output = self.cli("cancel", "sample")
            self.assertEqual(output.count("Cancellation requested"), 1, output)
            self.assertRegex(output, r"sample__gwflow_complete:.*Cancellation requested")
            self.settle()
        finally:
            (self.work / "manifest-release").touch()
        self.assertIn("Finish", self.cli("run", "sample", "--dry-run"))
        self.cli("run", "sample")
        self.settle()
        self.assertIn(attempt, self.cli("logs", "sample", "--details"))
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"left": 1, "right": 1, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_unavailable_live_state_does_not_hide_known_identity_and_conflicting_ids_are_rejected(self):
        held, release = self.work / "held", self.work / "release"
        self.configure_workflow(right_command=(
            f"touch {shlex.quote(str(held))}; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf right > same.txt"))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            self.wait_for(lambda: "sample__left: complete" in self.cli("logs", "sample"))
            index = self.cli("logs", "sample", "--details")
            right = re.search(r"sample__right: running; submission (\S+)", index).group(1)
            environment = self.inject(job_states={"sample__right": "UNKNOWN"},
                                      conflicting_tracking_prefix="sample__join", record_cancellations=True,
                                      accept_cancel_only=True)
            before = self.snapshot()
            output = self.cli("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
            self.assertIn("sample__join", output)
            self.assertIn("conflicting backend job IDs", output)
            self.assertIn(right, output)
            self.assertEqual(output.count("Cancellation requested"), 2, output)
            requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
            self.assertEqual(len(requested), 2)
            self.assertTrue(any(name.startswith("sample__right__") for name in requested))
            self.assertTrue(any(name.startswith("sample__gwflow_complete__") for name in requested))
            self.assertEqual(self.snapshot(), before)
            (self.work / "cancellation-requests.jsonl").unlink()
            environment = self.inject(fail_status_prefix="sample__right", record_cancellations=True,
                                      accept_cancel_only=True)
            preview = self.cli("-b", "recovery_fixture", "cancel", "sample", "--dry-run", env=environment)
            self.assertEqual(preview.count("Would request cancellation"), 3, preview)
            output = self.cli("-b", "recovery_fixture", "cancel", "sample", env=environment)
            self.assertEqual(output.count("Cancellation requested"), 3, output)
            requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
            self.assertEqual(len(requested), 3)
            self.assertTrue(any(name.startswith("sample__right__") for name in requested))
            self.assertEqual(self.snapshot(), before)
            (self.work / "cancellation-requests.jsonl").unlink()
            environment = self.inject(identity_host="different-worker", record_cancellations=True, accept_cancel_only=True)
            result = self.cli_result("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
            self.assertEqual(result.stdout.count("recorded backend does not match the selected backend"), 3, result.stdout)
            self.assertIn("recorded backend does not match the selected backend", result.stderr)
            self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
            self.assertEqual(self.snapshot(), before)
        finally:
            release.touch()
        self.finish()

    def test_known_admission_without_backend_tracking_cancels_and_retries_preserving_progress(self):
        held, release, trace = (self.work / name for name in ("held", "release", "trace"))
        self.configure_workflow(right_command=(
            f"echo right >> {shlex.quote(str(trace))}; touch {shlex.quote(str(held))}; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf right > same.txt"))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(shell_epilogue=True))
        self.wait_for(held.exists)
        try:
            self.wait_for(lambda: "sample__left: complete" in self.cli("logs", "sample"))
            index = self.cli("logs", "sample", "--details")
            attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
            submission = re.search(r"sample__right: running; submission (\S+)", index).group(1)
            # Model a lost tracking flush while retaining the durable acknowledgement.
            path = self.work / ".gwf/local-backend-tracked.json"
            tracking = json.loads(path.read_text())
            del tracking[submission]
            path.write_text(json.dumps(tracking))
            output = self.cli("cancel", "sample")
            self.assertEqual(output.count("Cancellation requested"), 3, output)
            self.settle()
        finally:
            release.touch()
        self.assertIn("Retry", self.cli("run", "sample", "--dry-run"))
        self.cli("run", "sample")
        self.settle()
        self.assertIn(attempt, self.cli("logs", "sample", "--details"))
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        self.assertEqual(Counter(trace.read_text().splitlines()), {"left": 1, "right": 2, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_unresolved_identity_and_rejected_request_do_not_stop_other_jobs(self):
        self.run_complete()
        index = self.cli("logs", "sample", "--details")
        submissions = dict(re.findall(r"(sample__\w+): complete; submission (\S+)", index))
        # A missing selected intent is a persistence fault, not a state lookup.
        for path in (self.work / ".gwf/gwflow").rglob("*-intent.json"):
            if json.loads(path.read_text())["submission"] == submissions["sample__left"]:
                path.unlink()
                break
        else:
            self.fail("No current left-job intent available for fault injection")
        environment = self.inject(job_states={"sample": "RUNNING"}, record_cancellations=True,
                                  reject_cancel_prefix="sample__right", accept_cancel_only=True)
        before = self.snapshot()
        preview = self.cli("-b", "recovery_fixture", "cancel", "sample", "--dry-run", env=environment, success=False)
        self.assertIn("Unresolved job identity", preview)
        self.assertEqual(preview.count("Would request cancellation"), 4, preview)
        self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
        output = self.cli("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
        self.assertIn("sample__left", output)
        self.assertIn("Unresolved job identity", output)
        self.assertNotIn("Submission: not submitted", output)
        self.assertIn("sample__right", output)
        self.assertIn("Cancellation request failed: injected cancellation rejection", output)
        self.assertEqual(output.count("Cancellation requested"), 3, output)
        requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
        self.assertCountEqual(requested, [submission for public, submission in submissions.items() if public != "sample__left"])
        self.assertIn("Traceback", output)
        self.assertEqual(self.snapshot(), before)

    def test_slurm_requests_return_while_active_and_preserve_inactivity_safeguards(self):
        held, release = self.work / "held", self.work / "release"
        self.configure_workflow(right_command=(
            f"touch {shlex.quote(str(held))}; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf right > same.txt"))
        environment = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=environment)
        self.wait_for(held.exists)
        try:
            self.wait_for(lambda: "sample__left: complete" in self.cli("-b", "slurm", "logs", "sample", env=environment))
            index = self.cli("-b", "slurm", "logs", "sample", "--details", env=environment)
            attempt = re.search(r"attempt ([0-9a-f]{32})", index).group(1)
            submitted = [json.loads(line) for line in (self.work / "slurm-submitted.jsonl").read_text().splitlines()]
            expected = {item["id"] for item in submitted
                        if any(item["name"].startswith("sample__" + local + "__")
                               for local in ("right", "join", "gwflow_complete"))}
            before = self.snapshot()
            preview = self.cli("-b", "slurm", "cancel", "sample", "--dry-run", env=environment)
            self.assertEqual(preview.count("Would request cancellation"), 3, preview)
            self.assertFalse((self.work / "slurm-cancelled.jsonl").exists())
            self.assertEqual(self.snapshot(), before)
            output = self.cli("-b", "slurm", "cancel", "sample", env=environment)
            self.assertEqual(output.count("Cancellation requested"), 3, output)
            self.assertIn("may still be stopping", output)
            requested = [json.loads(line)["id"] for line in (self.work / "slurm-cancelled.jsonl").read_text().splitlines()]
            self.assertCountEqual(requested, expected)
            self.assertEqual(self.snapshot(), before)
            self.assertIn("active work", self.cli("-b", "slurm", "run", "sample", "--force", env=environment, success=False))
            self.assertIn("Cleanup refused", self.cli("-b", "slurm", "clean", "--attempt", attempt, "--delete", env=environment, success=False))
            (self.work / "forgotten-slurm-history").touch()
            self.assertIn("unresolved submission", self.cli("-b", "slurm", "run", "sample", "--force", env=environment, success=False))
            self.assertIn("Cleanup refused", self.cli("-b", "slurm", "clean", "--attempt", attempt, "--delete", env=environment, success=False))
            self.assertEqual(self.snapshot(), before)
        finally:
            release.touch()
        self.finish()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        self.assertIn("Reuse", self.cli("-b", "slurm", "run", "sample", "--dry-run", env=environment))


class CancellationSelectionTests(LocalBackendTestCase):
    configure_workflow = test_run_selection.SelectedRunTests.configure_workflow
    inject = test_run_selection.SelectedRunTests.inject
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def test_every_selector_is_validated_before_any_request_and_bare_cancel_is_rejected(self):
        self.run_complete()
        environment = self.inject(job_states={name: "RUNNING" for name in "abcde"},
                                  record_cancellations=True, accept_cancel_only=True)
        before = self.snapshot()
        for options in ((), ("--dry-run",)):
            output = self.cli("-b", "recovery_fixture", "cancel", "b", "missing*", "a__compute", "empty_task",
                              *options, env=environment, success=False)
            self.assertIn("Unmatched Task selectors", output)
            for selector in ("missing*", "a__compute", "empty_task"):
                self.assertIn(repr(selector), output)
            self.assertNotIn("Cancellation requested", output)
            self.assertNotIn("Would request", output)
        bare = self.cli("-b", "recovery_fixture", "cancel", env=environment, success=False)
        self.assertIn("Missing argument", bare)
        self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
        self.assertEqual(self.snapshot(), before)

    def test_removed_tasks_remain_selectable_and_unstarted_tasks_are_successful_noops(self):
        self.run_complete()
        self.configure_workflow(tasks=(("fresh", ()),))
        environment = self.inject(job_states={name: "RUNNING" for name in "abcde"},
                                  record_cancellations=True, accept_cancel_only=True)
        before = self.snapshot()
        output = self.cli("-b", "recovery_fixture", "cancel", "b", "fresh", env=environment)
        self.assertIn("Task b: Cancellation outcomes; no longer declared", output)
        self.assertIn("Task fresh: No admitted work to cancel", output)
        requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
        self.assertEqual(len(requested), 3)
        self.assertTrue(all(name.startswith("b__") for name in requested))
        output = self.cli("-b", "recovery_fixture", "cancel", "*", "--dry-run", "--details", env=environment)
        self.assertEqual(set(re.findall(r"(?m)^Task (\w+):", output)), {*"abcde", "fresh"})
        self.assertEqual(output.count("no longer declared"), 5)
        self.assertEqual(output.count("Would request cancellation"), 15)
        self.configure(**{"backend.local.port": 0})
        for options in ((), ("--dry-run",)):
            self.assertIn("No admitted work to cancel", self.cli("cancel", "fresh", *options))
        self.assertEqual(self.snapshot(), before)

    def test_exact_multiple_patterns_and_all_address_only_selected_current_attempts(self):
        self.run_complete()
        previous = set(re.findall(r"; submission (\S+)", self.cli("logs", "--details")))
        self.cli("run", "--force")
        self.finish()
        current = set(re.findall(r"; submission (\S+)", self.cli("logs", "--details")))
        self.assertTrue(previous.isdisjoint(current))
        environment = self.inject(job_states={name: "RUNNING" for name in "abcde"},
                                  record_status=True, record_cancellations=True, accept_cancel_only=True)
        before = self.snapshot()
        requests = self.work / "cancellation-requests.jsonl"
        observations = self.work / "backend-observations.jsonl"
        for selectors, names in ((("b",), {"b"}), (("b", "d"), {"b", "d"}),
                                 (("[bd]", "b", "b"), {"b", "d"}), (("*",), set("abcde"))):
            with self.subTest(selectors=selectors):
                requests.unlink(missing_ok=True)
                observations.unlink(missing_ok=True)
                expected = {submission for submission in current if submission.split("__", 1)[0] in names}
                preview = self.cli("-b", "recovery_fixture", "cancel", *selectors, "--dry-run", "--details", env=environment)
                self.assertEqual(preview.count("Would request cancellation"), len(expected), preview)
                self.assertEqual(set(re.findall(r"(?m)^Task (\w+):", preview)), names)
                self.assertFalse(requests.exists())
                output = self.cli("-b", "recovery_fixture", "cancel", *selectors, env=environment)
                self.assertEqual(set(re.findall(r"(?m)^Task (\w+):", output)), names)
                requested = [json.loads(line) for line in requests.read_text().splitlines()]
                self.assertCountEqual(requested, expected)
                observed = {json.loads(line) for line in observations.read_text().splitlines()}
                self.assertEqual(observed, expected)
                self.assertEqual(self.snapshot(), before)
