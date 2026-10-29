"""Recorded job identities and outcomes through the installed status command."""

import json
import shlex

from record_fixtures import read_record, record_path, remove_record, write_record
from support import LocalBackendTestCase
import test_fresh
import test_graphs
import test_inspection


class JsonJobTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    snapshot = test_inspection.LifecycleInspectionTests.snapshot
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle

    def report(self, *options, env=None):
        before = self.snapshot()
        backend = ("-b", "recovery_fixture") if env is not None else ()
        result = self.cli_result(*backend, "status", "--json", *options, env=env)
        self.assertEqual(self.snapshot(), before)
        return json.loads(result.stdout)

    def test_details_include_complete_records_without_allocating_submissions(self):
        summary = self.report()
        detailed = self.report("--details")
        self.assertEqual(detailed["notices"], summary["notices"])
        for task, original in zip(detailed["tasks"], summary["tasks"], strict=True):
            jobs = task.pop("jobs")
            self.assertEqual(task, original)
            self.assertEqual(jobs, [
                {"name": task["name"] + "__" + local, "submission_name": None,
                 "backend_id": None, "state": "pending", "observed_state": None,
                 "in_current_definition": True}
                for local in ("gwflow_prepare", "compute", "gwflow_complete")])
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        selected = self.report("b*", "--details", "--status", "pending")
        self.assertEqual([row["name"] for row in selected["tasks"]], ["b"])
        self.assertEqual(len(selected["tasks"][0]["jobs"]), 3)
        self.assertEqual(self.report("--details", "--status", "reusable")["tasks"], [])

    def test_details_keep_output_streams_and_errors_machine_readable(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() + "\nprint('workflow diagnostic')\n")
        result = self.cli_result("status", "a", "--json", "--details")
        self.assertEqual(len(json.loads(result.stdout)["tasks"][0]["jobs"]), 3)
        self.assertIn("workflow diagnostic", result.stderr)
        terminal = self.terminal_cli("status", "a", "--json", "--details")
        self.assertEqual(len(json.loads(terminal.stdout)["tasks"][0]["jobs"]), 3)
        self.assertNotIn("\x1b", terminal.stdout)
        invalid = self.cli_result("status", "--json", "--details", "--unknown", success=False)
        self.assertEqual(invalid.returncode, 2)
        document = json.loads(invalid.stdout)
        self.assertEqual(set(document), {"schema_version", "error"})
        self.assertEqual(document["error"]["code"], "usage_error")
        workflow.write_text(workflow.read_text() + "\nraise RuntimeError('loading failed')\n")
        failed = self.cli_result("status", "--json", "--details", success=False)
        self.assertEqual(failed.returncode, 1)
        document = json.loads(failed.stdout)
        self.assertEqual(set(document), {"schema_version", "error"})
        self.assertEqual(document["error"]["code"], "inspection_failed")
        self.assertIn("workflow diagnostic", failed.stderr)

    def test_reuse_does_not_replace_the_recorded_submission_assessment(self):
        self.run_complete("a")
        # Checked target metadata still supports the retained results, but no
        # longer certifies this admission's execution outcome.
        intent = read_record(record_path(self.work, "a", "submissions/*/compute-intent.json"))
        remove_record(record_path(self.work, "a", f"admissions/{intent['admission']}/outcome.json"))
        success = record_path(self.work, "a", "executions/*/success.json")
        write_record(success, {**read_record(success), "admission": "0" * 32})
        row = self.report("a", "--details", env=self.inject(job_states={"a__compute": "CANCELLED"}))["tasks"][0]
        self.assertEqual(row["state"], "reusable")
        self.assertEqual(row["progress"], {"completed": 3, "total": 3})
        compute = next(job for job in row["jobs"] if job["name"] == "a__compute")
        self.assertEqual(compute["state"], "completed")
        self.assertEqual(compute["observed_state"], "canceled")
        self.assertEqual(compute["submission_name"], intent["submission"])

    def test_known_rejection_keeps_submission_name_without_backend_acceptance(self):
        self.cli("-b", "recovery_fixture", "run", "a",
                 env=self.inject(reject_before_admission=True), success=False)
        row = self.report("a", "--details")["tasks"][0]
        prepare, compute, complete = row["jobs"]
        self.assertEqual(row["next_action"], "continue")
        self.assertEqual(row["progress"], {"completed": 0, "total": 3})
        self.assertIsInstance(prepare["submission_name"], str)
        self.assertIsNone(prepare["backend_id"])
        self.assertEqual((prepare["state"], prepare["observed_state"]), ("pending", "pending"))
        for job in (compute, complete):
            self.assertIsNone(job["submission_name"])
            self.assertIsNone(job["backend_id"])
            self.assertIsNone(job["observed_state"])
            self.assertEqual(job["state"], "pending")

    def test_unresolved_admission_is_unknown_and_keeps_notices_when_filtered(self):
        self.cli("-b", "recovery_fixture", "run", "a",
                 env=self.inject(reject_prefix="a__gwflow_prepare"), success=False)
        document = self.report("a", "--details")
        row = document["tasks"][0]
        prepare = row["jobs"][0]
        self.assertIsInstance(prepare["submission_name"], str)
        self.assertIsNone(prepare["backend_id"])
        self.assertEqual((prepare["state"], prepare["observed_state"]), ("unknown", "unknown"))
        self.assertEqual(row["progress"], {"completed": None, "total": 3})
        filtered = self.report("a", "--details", "--status", "reusable")
        self.assertEqual(filtered["tasks"], [])
        self.assertEqual(filtered["notices"], document["notices"])
        self.assertEqual({notice["code"] for notice in filtered["notices"]},
                         {"workflow_blocked", "submission_investigation_required"})

    def test_checked_completion_resolves_unknown_and_keeps_old_identities_for_repetition(self):
        self.cli("-b", "recovery_fixture", "run", "a", env=self.inject(record_acceptances=True))
        self.finish()
        accepted = {item["name"]: str(item["id"]) for item in
                    map(json.loads, (self.work / "backend-acceptances.jsonl").read_text().splitlines())}
        row = self.report("a", "--details", env=self.inject(job_states={"a": "UNKNOWN"}))["tasks"][0]
        self.assertEqual(row["state"], "reusable")
        self.assertEqual(row["progress"], {"completed": 3, "total": 3})
        self.assertEqual({job["submission_name"]: job["backend_id"] for job in row["jobs"]}, accepted)
        for job in row["jobs"]:
            self.assertEqual((job["state"], job["observed_state"]), ("completed", "completed"))
            self.assertIsInstance(job["backend_id"], str)
            self.assertTrue(job["in_current_definition"])
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a", "printf UPDATED_A"))
        repeated = self.report("a", "--details", env=self.inject(job_states={"a": "UNKNOWN"}))["tasks"][0]
        self.assertEqual(repeated["attempt_id"], row["attempt_id"])
        self.assertEqual(repeated["next_action"], "run")
        self.assertEqual(repeated["progress"], {"completed": 0, "total": 3})
        self.assertEqual(repeated["jobs"], [{**job, "state": "pending"} for job in row["jobs"]])

    def test_integer_backend_id_is_a_string_without_a_false_identity_conflict(self):
        self.run_complete("a")
        intent = read_record(record_path(self.work, "a", "submissions/*/compute-intent.json"))
        acknowledgement = record_path(self.work, "a", f"admissions/{intent['admission']}/ack.json")
        write_record(acknowledgement, {**read_record(acknowledgement), "job_id": 12345})
        # A different backend identity leaves only the recorded, integer ID.
        row = self.report("a", "--details", env=self.inject(identity_host="another-host"))["tasks"][0]
        self.assertEqual(row["state"], "reusable")
        compute = next(job for job in row["jobs"] if job["name"] == "a__compute")
        self.assertEqual(compute["backend_id"], "12345")
        self.assertEqual(compute["submission_name"], intent["submission"])
        self.assertEqual(compute["observed_state"], "completed")

    def test_failed_job_keeps_equal_states_and_recorded_identity_until_actual_retry(self):
        failing = self.work / "fail-right"
        failing.touch()
        test_graphs.TaskGraphTests.configure_workflow(
            self, right_command=f"if [ -e {shlex.quote(str(failing))} ]; then exit 8; fi; printf right > same.txt")
        environment = self.inject(record_acceptances=True)
        self.cli("-b", "recovery_fixture", "run", env=environment)
        self.settle()
        accepted = (self.work / "backend-acceptances.jsonl").read_text()
        row = self.report("sample", "--details")["tasks"][0]
        jobs = {job["name"]: job for job in row["jobs"]}
        self.assertEqual(row["next_action"], "retry")
        self.assertEqual(row["progress"], {"completed": 2, "total": 5})
        failed = jobs["sample__right"]
        self.assertEqual((failed["state"], failed["observed_state"]), ("failed", "failed"))
        self.assertEqual(jobs["sample__left"]["state"], "completed")
        self.assertEqual({job["submission_name"]: job["backend_id"] for job in row["jobs"]},
                         {item["name"]: str(item["id"]) for item in map(json.loads, accepted.splitlines())})
        self.assertEqual(self.report("sample", "--details")["tasks"][0], row)
        self.assertEqual((self.work / "backend-acceptances.jsonl").read_text(), accepted)
        failing.unlink()
        self.cli("-b", "recovery_fixture", "run", env=environment)
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")
        retried = self.report("sample", "--details")["tasks"][0]
        current = {job["name"]: job for job in retried["jobs"]}
        self.assertEqual(retried["attempt_id"], row["attempt_id"])
        self.assertEqual(retried["state"], "reusable")
        self.assertEqual(retried["progress"], {"completed": 5, "total": 5})
        self.assertEqual(current["sample__left"], jobs["sample__left"])
        self.assertEqual(current["sample__right"]["state"], "completed")
        self.assertNotEqual(current["sample__right"]["submission_name"], failed["submission_name"])
        self.assertNotEqual(current["sample__right"]["backend_id"], failed["backend_id"])
        self.assertEqual(len(current), 5)
        self.assertEqual(len((self.work / "backend-acceptances.jsonl").read_text().splitlines()), 8)

    def test_current_active_and_terminal_jobs_use_public_states(self):
        self.run_complete("a")
        for local, backend_state, expected in (
                ("gwflow_prepare", "RUNNING", "running"),
                ("compute", "SUBMITTED", "queued"),
                ("gwflow_complete", "RUNNING", "running"),
                ("gwflow_complete", "FAILED", "failed"),
                ("gwflow_complete", "CANCELLED", "canceled")):
            with self.subTest(local=local, state=expected):
                name = "a__" + local
                environment = self.inject(job_states={name: backend_state})
                row = self.report("a", "--details", env=environment)["tasks"][0]
                job = next(job for job in row["jobs"] if job["name"] == name)
                self.assertEqual((job["state"], job["observed_state"]), (expected, expected))
                self.assertIsInstance(job["submission_name"], str)
                self.assertIsInstance(job["backend_id"], str)
                self.assertTrue(job["in_current_definition"])
                self.assertEqual(row["progress"], {"completed": 2, "total": 3})
        filtered = self.report("a", "--details", "--status", "cancelled", env=environment)
        self.assertEqual(filtered["tasks"], [row])

    def test_unavailable_observations_keep_requested_fields_null(self):
        self.run_complete("a")
        original = self.report("a", "--details")["tasks"][0]
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "outputs=['private.txt'])", "outputs=['private.txt'], image='missing.sif')", 1))
        row = self.report("a", "--details")["tasks"][0]
        self.assertEqual(row["attempt_id"], original["attempt_id"])
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["progress"], {"completed": None, "total": 3})
        for job in row["jobs"]:
            self.assertEqual(job["state"], "unknown")
            self.assertIsNone(job["observed_state"])
            self.assertIsNone(job["submission_name"])
            self.assertIsNone(job["backend_id"])

    def test_removed_jobs_remain_visible_only_while_active_or_unresolved(self):
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        workflow.write_text(original.replace(
            "a = gwf.task", "removed = task.target('removed', inputs=[], outputs=['old.txt'])\n"
            "removed << 'touch old.txt'\na = gwf.task"))
        self.run_complete("a")
        before = self.report("a", "--details")["tasks"][0]
        old = next(job for job in before["jobs"] if job["name"] == "a__removed")
        workflow.write_text(original)
        for options, expected, completed in (
                ({"queued_prefix": "a__removed"}, "queued", 0),
                ({"running_prefix": "a__removed"}, "running", 0),
                ({"conflicting_tracking_prefix": "a__removed"}, "unknown", 3)):
            with self.subTest(state=expected):
                row = self.report("a", "--details", env=self.inject(**options))["tasks"][0]
                self.assertEqual(row["attempt_id"], before["attempt_id"])
                self.assertEqual(row["progress"], {"completed": completed, "total": 3})
                self.assertEqual(len(row["jobs"]), 4)
                removed = next(job for job in row["jobs"] if job["name"] == "a__removed")
                self.assertEqual(removed, {**old, "state": expected, "observed_state": expected,
                                           "in_current_definition": False})
                self.assertTrue(all(job["in_current_definition"] for job in row["jobs"] if job != removed))
        row = self.report("a", "--details")["tasks"][0]
        self.assertEqual(row["attempt_id"], before["attempt_id"])
        self.assertEqual(row["progress"], {"completed": 0, "total": 3})
        self.assertEqual([job["name"] for job in row["jobs"]],
                         ["a__gwflow_prepare", "a__compute", "a__gwflow_complete"])
        self.run_complete("a")
        refreshed = self.report("a", "--details", env=self.inject(running_prefix="a__removed"))["tasks"][0]
        self.assertNotEqual(refreshed["attempt_id"], before["attempt_id"])
        self.assertEqual(refreshed["state"], "reusable")
        self.assertEqual(refreshed["progress"], {"completed": 3, "total": 3})
        self.assertNotIn("a__removed", [job["name"] for job in refreshed["jobs"]])
