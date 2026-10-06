"""Planning I/O and observation lifetime through the installed CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from support import FIXTURES, LocalBackendTestCase
import test_fresh


class PlanningObservationTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow

    def probe(self, *arguments):
        report = self.work / "probe.json"
        result = subprocess.run([sys.executable, str(FIXTURES / "planning_probe.py"), str(report), *arguments],
                                cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        measured = json.loads(report.read_text())
        if destination := os.environ.get("GWFLOW_IO_EVIDENCE"):
            root = Path(destination)
            root.mkdir(parents=True, exist_ok=True)
            (root / ("-".join(arguments).replace("/", "_") + ".json")).write_text(report.read_text())
        return result.stdout, measured

    def test_consumer_and_replacement_checks_share_job_evidence(self):
        self.run_complete()
        for command in (("status",), ("status", "--details", "c"), ("explain",),
                        ("run", "--dry-run"), ("run",), ("explain", "--force-task", "a")):
            with self.subTest(command=command):
                output, report = self.probe(*command)
                self.assertEqual(output, self.cli_result(*command).stdout)
                ack_reads = {path: count for path, count in report["paths"]["planning"].items()
                             if path.startswith("read:") and path.endswith("/ack.json")}
                self.assertEqual(len(ack_reads), 9)
                self.assertEqual(set(ack_reads.values()), {1}, ack_reads)
                self.assertEqual(report["counts"]["backend"]["scheduler_requests"], 1)

    def new_admission(self):
        selected = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/c/attempts/*/submissions/*/compute-intent.json"))
        original = json.loads(selected.read_text())
        changed = {**original, "admission": uuid4().hex}
        changed["submission"] = changed["job"] + "__" + changed["admission"]
        attempt = selected.parents[2]
        archive = attempt / "admissions" / changed["admission"] / "intent.json"
        ack = attempt / "admissions" / original["admission"] / "ack.json"
        return selected, original, {str(selected): changed, str(archive): changed}, str(ack)

    def scenario(self, commands, actions):
        path = self.work / "scenario.json"
        path.write_text(json.dumps({"commands": commands, "actions": actions}))
        result = subprocess.run([sys.executable, str(FIXTURES / "planning_fault.py"), str(path)],
                                cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["unreached_actions"], [])
        return report["results"]

    def test_changed_admission_during_inspection_protects_its_producer(self):
        self.run_complete()
        _, _, writes, ack = self.new_admission()
        result, = self.scenario([["explain", "--force-task", "a"]],
                               [{"pass": 0, "after": ack, "write": writes}])
        self.assertEqual(result["exit_code"], 0)
        self.assertRegex(result["output"], r"Task a\s+Blocked\s+")
        self.assertIn("active consumers", result["output"])
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")

    def test_new_selected_execution_preparation_and_operation_are_observed(self):
        self.run_complete()
        attempt_path = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/c/attempts/*/attempt.json"))
        original = attempt_path.read_text()
        for local, field in (("compute", "execution"), ("gwflow_prepare", "preparation"),
                             ("gwflow_complete", "operation")):
            with self.subTest(local=local):
                attempt = json.loads(original)
                old_generation = attempt["jobs"][local].rsplit("__", 1)[1]
                selected = attempt_path.parent / "submissions" / old_generation / (local + "-intent.json")
                intent = json.loads(selected.read_text())
                ack = attempt_path.parent / "admissions" / intent["admission"] / "ack.json"
                generation, token = uuid4().hex, uuid4().hex
                if field == "execution":
                    attempt["executions"][local] = generation
                else:
                    attempt[field] = generation
                job = f"c__{local}__{generation}"
                attempt["jobs"][local] = job
                intent.update({field: generation, "job": job, "admission": token, "submission": job + "__" + token})
                writes = {str(attempt_path): attempt,
                          str(attempt_path.parent / "submissions" / generation / selected.name): intent,
                          str(attempt_path.parent / "admissions" / token / "intent.json"): intent}
                result, = self.scenario([["explain", "--force-task", "a"]],
                                       [{"pass": 0, "after": str(ack), "write": writes}])
                self.assertEqual(result["exit_code"], 0)
                self.assertRegex(result["output"], r"Task a\s+Blocked\s+")
                self.assertIn("active consumers", result["output"])
                attempt_path.write_text(original)

    def test_submission_rechecks_observations_after_planning_on_the_same_store(self):
        self.run_complete()
        _, _, writes, _ = self.new_admission()
        result, = self.scenario([["run", "--force-task", "a"]],
                               [{"pass": 0, "after": "planned", "write": writes}])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("Active consumers block replacement", result["output"])
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        self.assertEqual((self.work / "trace").read_text().splitlines().count("a"), 1)

    def test_changed_acknowledgement_with_the_same_admission_blocks_submission(self):
        self.run_complete()
        _, _, _, ack = self.new_admission()
        changed = json.loads(Path(ack).read_text())
        changed["job_id"] = "different-scheduler-job"
        run, status = self.scenario([["run", "--force-task", "a"], ["status", "c"]],
                                   [{"pass": 0, "after": "planned", "write": {ack: changed}}])
        self.assertNotEqual(run["exit_code"], 0)
        self.assertIn("Active consumers block replacement", run["output"])
        self.assertEqual(status["exit_code"], 0)
        self.assertIn("unresolved submission", status["output"])
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        self.assertEqual((self.work / "trace").read_text().splitlines().count("a"), 1)

    def test_later_cli_pass_observes_publication_after_success_error_or_interruption(self):
        self.run_complete()
        for failure in (None, "error", "interrupt"):
            with self.subTest(failure=failure):
                selected, original, writes, ack = self.new_admission()
                actions = [{"pass": 0, "after": ack if failure else "planned", "write": writes,
                            **({failure: True} if failure else {})}]
                first, second = self.scenario([["status"], ["status", "c"]], actions)
                self.assertEqual(first["exit_code"], 0 if failure is None else 1)
                self.assertEqual(second["exit_code"], 0)
                self.assertIn("State: blocked", second["output"])
                self.assertIn("unresolved submission", second["output"])
                selected.write_text(json.dumps(original))
