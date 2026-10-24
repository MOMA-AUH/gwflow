"""Consolidated evidence through installed commands and actual local workers."""

import json
import re
import shlex
import shutil

from gwf.exceptions import WorkflowError
from gwflow import _observations, admission
from gwflow.lifecycle import Store

from support import LocalBackendTestCase
import test_graphs
import test_managed_recovery
import test_interactive_status
import test_planning_io
import test_recovery


class ConsolidatedRecordTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    interrupt_computation = test_recovery.OperatorRecoveryTests.interrupt_computation
    probe = test_planning_io.PlanningObservationTests.probe
    launch = test_managed_recovery.ManagedCoordinationTests.launch
    terminal = test_interactive_status.InteractiveStatusTests.terminal

    def compact_fixture(self, name="sample"):
        store = Store(self.work)
        attempt = store.recorded_current(name)
        root = next((self.work / ".gwf/gwflow").glob(f"owners/*/tasks/{name}/attempts/{attempt['attempt']}"))
        state = json.loads((root / "record-state.json").read_text())
        self.assertEqual(state["phase"], "compacted")
        return store, attempt, root, state

    def test_inspection_logs_and_superseded_attempt_keep_exact_history(self):
        failing = self.work / "fail"
        failing.touch()
        self.configure_workflow(right_command=f"echo historical-output; test ! -e {shlex.quote(str(failing))}; printf right > same.txt")
        self.cli("run")
        self.settle()
        first = self.cli("logs", "sample", "--details")
        submission = re.search(r"sample__right: failed; submission (\S+)", first).group(1)
        failing.unlink()
        self.cli("run")
        self.settle()
        _, _, root, state = self.compact_fixture()
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        self.assertIn("reusable", self.cli("status", "--details"))
        self.assertIn("Reuse", self.cli("run", "--dry-run", "--details"))
        history = self.cli("logs", "sample", "--history", "--details")
        self.assertIn(submission, history)
        self.assertIn("historical-output", self.cli("logs", submission, "--no-pager"))
        self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
        # Expired scheduler history must leave durable outcomes inspectable.
        env = self.inject(job_states={"sample": "UNKNOWN"})
        self.assertIn("reusable", self.cli("-b", "recovery_fixture", "status", env=env))
        self.assertIn(submission, self.cli("-b", "recovery_fixture", "logs", "sample", "--history", env=env))
        self.cli("run", "--force")
        self.settle()
        self.assertIn(submission, self.cli("logs", "sample", "--history"))
        self.assertIn("historical-output", self.cli("logs", submission, "--no-pager"))
        self.assertEqual(json.loads((root / "record-state.json").read_text()), state)

    def test_recoveries_and_worker_outcomes_update_consolidated_records(self):
        self.interrupt_computation()
        self.cli("-b", "recovery_fixture", "recover", "sample", env=self.inject(job_states={"sample": "UNKNOWN"}))
        self.cli("run")
        self.settle()
        _, _, root, state = self.compact_fixture()
        recoveries = {name: value for name, value in state["records"].items() if name.startswith("recoveries/")}
        self.assertEqual(len(recoveries), 3)
        self.cli("clean", "--delete", "sample")
        self.assertIn("Reuse", self.cli("run", "--dry-run"))
        updated = json.loads((root / "record-state.json").read_text())
        self.assertEqual(updated["phase"], "compacted")
        self.assertEqual({name: updated["records"][name] for name in recoveries}, recoveries)
        self.assertGreater(len(updated["records"]), len(state["records"]))
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["record-state.json", "records.lock"])
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "leftright")

    def test_changed_evidence_refreshes_completion_and_admissions_in_one_store(self):
        self.run_complete()
        store, attempt, _, _ = self.compact_fixture()
        self.assert_observation_refreshes(store, attempt)

    def test_changed_loose_evidence_refreshes_completion_and_admissions_in_one_store(self):
        self.run_complete()
        store = Store(self.work)
        attempt = store.recorded_current("sample")
        store.records(attempt).restore()
        self.assert_observation_refreshes(store, attempt)

    def assert_observation_refreshes(self, store, attempt):
        peer = Store(self.work)
        completion = store.read(attempt, "completion.json", "completion")
        with _observations.planning_pass():
            self.assertTrue(store.completed(attempt))
            observed = admission.observe(store, attempt, None, "local")
            self.assertEqual(observed["right"].state, "complete")
            intent = observed["right"].intent
            peer.publish(attempt, f"admissions/{intent['admission']}/ack.json", "submission-ack",
                         **admission.identity(intent), job_id=99999)
            self.assertEqual(admission.observe(store, attempt, None, "local")["right"].job_id, 99999)
            peer.records(attempt).publish("completion.json", {**completion, "outputs": {}})
            self.assertFalse(store.completed(attempt))

    def test_malformed_or_missing_authority_never_uses_redundant_records(self):
        self.run_complete()
        store, attempt, root, state = self.compact_fixture()
        # Leave stale originals under a damaged authority to prove no fallback.
        store.records(attempt).restore()
        (root / "record-state.json").write_text(json.dumps(state))
        path = root / "record-state.json"
        original = path.read_bytes()
        for damaged in (b"{broken", None):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    path.unlink()
                else:
                    path.write_bytes(damaged)
                for command in (("status", "--details"), ("run", "--dry-run"), ("logs", "sample", "--history")):
                    self.assertIn("record", self.cli(*command, success=False))
                path.write_bytes(original)
        self.assertIn("Reuse", self.cli("run", "--dry-run"))

    def test_planning_decodes_each_unchanged_consolidated_document_once(self):
        self.run_complete()
        _, _, root, _ = self.compact_fixture()
        output, report = self.probe("status", "--details")
        self.assertIn("reusable", output)
        self.assertEqual(report["paths"]["planning"]["read:" + str(root / "record-state.json")], 1)

    def test_interactive_status_reads_consolidated_job_evidence(self):
        self.run_complete()
        self.compact_fixture()
        session = self.terminal("status")
        session.wait_for("sample")
        checkpoint = session.send("\r")
        session.wait_for("Instance", after=checkpoint)
        checkpoint = session.send("\r")
        session.wait_for("Jobs completed: 5/5", after=checkpoint)
        checkpoint = session.send("\r")
        session.wait_for("Public job:     sample__gwflow_prepare", after=checkpoint)
        session.send("q")
        self.assertEqual(session.finish(), 0, session.text)

    def test_worker_finishes_ahead_of_frontend_acknowledgement_and_keeps_all_evidence(self):
        process, output = self.launch("-b", "recovery_fixture", "run",
                                      env=self.inject(hold_after_acceptance="sample__gwflow_complete"))
        self.wait_for(lambda: (self.work / "acceptance-held").exists())
        try:
            # The frontend still holds its workflow guard while real workers
            # finish, demonstrating that workers use only attempt locks.
            self.finish()
            store, attempt, root, state = self.compact_fixture()
            intent = admission.read_intent(store, attempt, "gwflow_complete")
            ack = f"admissions/{intent['admission']}/ack.json"
            self.assertNotIn(ack, state["records"])
            inode = (root / "records.lock").stat().st_ino
            self.assertEqual(store.read(attempt, f"admissions/{intent['admission']}/outcome.json",
                                        "job-outcome")["state"], "complete")
        finally:
            (self.work / "acceptance-release").touch()
        self.assertEqual(process.wait(timeout=15), 0, output.read_text())
        self.assertIsNotNone(store.read(attempt, ack, "submission-ack"))
        updated = json.loads((root / "record-state.json").read_text())
        self.assertEqual(updated["phase"], "compacted")
        self.assertEqual({name: updated["records"][name] for name in state["records"]}, state["records"])
        self.assertEqual((root / "records.lock").stat().st_ino, inode)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["record-state.json", "records.lock"])
        self.assertIn("Reuse", self.cli("run", "--dry-run"))


class ConsolidatedProducerTests(LocalBackendTestCase):
    configure_workflow = test_planning_io.ProducerEvidenceTests.configure_workflow
    compact_fixture = ConsolidatedRecordTests.compact_fixture

    def test_real_consumer_worker_reads_consolidated_producer_after_work_cleanup(self):
        self.run_complete("reference")
        self.compact_fixture("reference")
        shutil.rmtree(self.work / "work/reference")
        self.run_complete("sample0")
        self.assertEqual((self.work / "results/sample0/copy.txt").read_text(), "hello\n")
        self.assertIn("2 reusable", self.cli("status"))

    def test_consumer_rechecks_changed_producer_completion_within_one_store_observation(self):
        self.run_complete()
        store, producer, _, _ = self.compact_fixture("reference")
        _, consumer, _, _ = self.compact_fixture("sample0")
        peer = Store(self.work)
        with _observations.planning_pass():
            self.assertTrue(store.completed(consumer))
            completion = peer.read(producer, "completion.json", "completion")
            peer.records(producer).publish("completion.json", {**completion, "outputs": {}})
            self.assertFalse(store.completed(producer))
            with self.assertRaisesRegex(WorkflowError, "lacks checked Completion"):
                store.completed(consumer)
