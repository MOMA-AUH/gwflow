"""Managed cancellation through the installed CLI and real backend boundaries."""

from collections import Counter
import json
import re
import shlex
import shutil

from support import FIXTURES, LocalBackendTestCase
import test_graphs
import test_inspection
import test_run_selection


class CancellationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    slurm_environment = test_graphs.TaskGraphTests.slurm_environment
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def test_inactive_jobs_are_distinguished_from_unavailable_live_state(self):
        self.run_complete()
        before = self.snapshot()
        output = self.cli("cancel", "sample")
        self.assertEqual(output.count("backend confirms inactivity"), 5, output)
        self.assertIn("No admitted work to cancel", output)
        environment = self.inject(job_states={"sample": "UNKNOWN"}, record_cancellations=True)
        output = self.cli("-b", "recovery_fixture", "cancel", "sample", env=environment)
        self.assertNotIn("backend confirms inactivity", output)
        self.assertEqual(output.count("completed work preserved; live state unavailable"), 5, output)
        self.assertFalse((self.work / "cancellation-requests.jsonl").exists())
        self.assertEqual(self.snapshot(), before)

    def test_cancelled_completion_recovers_without_repeating_computation(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_after_manifest": True}))
        environment = self.inject(job_fault="sample__gwflow_complete")
        self.cli("-b", "recovery_fixture", "run", env=environment)
        self.wait_for((self.work / "manifest-held").exists)
        try:
            index = self.cli("logs", "sample")
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
        self.assertIn(attempt, self.cli("logs", "sample"))
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
            environment = self.inject(job_states={"sample__right": "UNKNOWN"},
                                      conflicting_tracking_prefix="sample__join", record_cancellations=True,
                                      accept_cancel_only=True)
            before = self.snapshot()
            output = self.cli("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
            self.assertIn("sample__join", output)
            self.assertIn("conflicting backend job IDs", output)
            self.assertEqual(output.count("Cancellation requested"), 2, output)
            requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
            self.assertEqual(len(requested), 2)
            self.assertTrue(any(name.startswith("sample__right__") for name in requested))
            self.assertTrue(any(name.startswith("sample__gwflow_complete__") for name in requested))
            self.assertEqual(self.snapshot(), before)
            (self.work / "cancellation-requests.jsonl").unlink()
            environment = self.inject(identity_host="different-worker", record_cancellations=True, accept_cancel_only=True)
            output = self.cli("-b", "recovery_fixture", "cancel", "sample", env=environment, success=False)
            self.assertEqual(output.count("recorded backend does not match the selected backend"), 3, output)
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
        self.cli("run")
        self.wait_for(held.exists)
        try:
            self.wait_for(lambda: "sample__left: complete" in self.cli("logs", "sample"))
            index = self.cli("logs", "sample")
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
        self.assertIn(attempt, self.cli("logs", "sample"))
        self.assertIn("Reuse", self.cli("run", "sample", "--dry-run"))
        self.assertEqual(Counter(trace.read_text().splitlines()), {"left": 1, "right": 2, "join": 1})
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_unresolved_identity_and_rejected_request_do_not_stop_other_jobs(self):
        self.run_complete()
        index = self.cli("logs", "sample")
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
        self.assertIn("sample__right", output)
        self.assertIn("Cancellation request failed: injected cancellation rejection", output)
        self.assertEqual(output.count("Cancellation requested"), 3, output)
        requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
        self.assertCountEqual(requested, [submission for public, submission in submissions.items() if public != "sample__left"])
        self.assertNotIn("Traceback", output)
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
            index = self.cli("-b", "slurm", "logs", "sample", env=environment)
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
        self.assertRegex(output, r"Task b: current attempt \w+; no longer declared")
        self.assertIn("Task fresh: no admitted work to cancel", output)
        requested = [json.loads(line) for line in (self.work / "cancellation-requests.jsonl").read_text().splitlines()]
        self.assertEqual(len(requested), 3)
        self.assertTrue(all(name.startswith("b__") for name in requested))
        output = self.cli("-b", "recovery_fixture", "cancel", "*", "--dry-run", env=environment)
        self.assertEqual(set(re.findall(r"(?m)^Task (\w+):", output)), {*"abcde", "fresh"})
        self.assertEqual(output.count("no longer declared"), 5)
        self.assertEqual(output.count("Would request cancellation"), 15)
        self.configure(**{"backend.local.port": 0})
        for options in ((), ("--dry-run",)):
            self.assertIn("no admitted work to cancel", self.cli("cancel", "fresh", *options))
        self.assertEqual(self.snapshot(), before)

    def test_exact_multiple_patterns_and_all_address_only_selected_current_attempts(self):
        self.run_complete()
        previous = set(re.findall(r"; submission (\S+)", self.cli("logs")))
        self.cli("run", "--force")
        self.finish()
        current = set(re.findall(r"; submission (\S+)", self.cli("logs")))
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
                preview = self.cli("-b", "recovery_fixture", "cancel", *selectors, "--dry-run", env=environment)
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
