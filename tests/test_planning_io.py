"""Planning I/O and observation lifetime through the installed CLI."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from support import TASK_FACTORY, FIXTURES, LocalBackendTestCase
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
            label = getattr(self, "evidence_label", "") + "-".join(arguments).replace("/", "_")
            (root / (label + ".json")).write_text(report.read_text())
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
        self.assertIn("submission outcome unknown", status["output"])
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
                self.assertRegex(second["output"], r"Task c\s+blocked")
                self.assertIn("submission outcome unknown", second["output"])
                selected.write_text(json.dumps(original))


class ProducerEvidenceTests(LocalBackendTestCase):
    workers = 4
    probe = PlanningObservationTests.probe
    scenario = PlanningObservationTests.scenario

    def configure_workflow(self, consumers=1, references=1):
        source = (
            TASK_FACTORY + "from gwflow import Task, Workflow, shell\n"
            "gwf = Workflow()\n"
            "task = empty_task(inputs=['input.txt'])\n"
            "target = task.target('compute', inputs=task.inputs, outputs=['value.txt'])\n"
            "target << shell('cat {source} > value.txt', source='input.txt')\n"
        )
        for index in range(references):
            source += f"task.retain('value{index}', source=target.output('value.txt'), path='value{index}.txt')\n"
        source += "reference = gwf.task(task, alias='reference')\n"
        incoming = ", ".join(f"reference.outputs['value{index}']" for index in range(references))
        parameters = ", ".join(f"i{index}=reference.outputs['value{index}']" for index in range(references))
        command = "cat " + " ".join("{i" + str(index) + "}" for index in range(references)) + " > copy.txt"
        for index in range(consumers):
            source += (
                f"task = empty_task(inputs=[{incoming}])\n"
                "target = task.target('compute', inputs=task.inputs, outputs=['copy.txt'])\n"
                f"target << shell({command!r}, {parameters})\n"
                "task.retain('copy', source=target.output('copy.txt'), path='copy.txt')\n"
                f"sample{index} = gwf.task(task, alias='sample{index}')\n"
            )
        (self.work / "workflow.py").write_text(source)
        self.evidence_label = f"consumers-{consumers}-references-{references}-"

    def check_shared_evidence(self, consumers, references):
        self.configure_workflow(consumers, references)
        self.run_complete()
        output, report = self.probe("status")
        self.assertEqual(output, self.cli_result("status").stdout)
        self.assertEqual(output.count("reusable"), consumers + 2)
        producer_reads = {path: count for path, count in report["paths"]["planning"].items()
                          if path.startswith("read:") and "/tasks/reference/" in path}
        self.assertTrue(producer_reads)
        self.assertEqual(max(producer_reads.values()), 1)
        self.assertLessEqual(report["paths"]["planning"].get("stat:" + str(self.work / "input.txt"), 0), 2)

    def test_single_consumer_reads_each_producer_record_once(self):
        self.check_shared_evidence(1, 1)

    def test_more_consumers_share_complete_producer_validation(self):
        self.check_shared_evidence(4, 1)

    def test_multiple_retained_references_share_producer_evidence(self):
        self.check_shared_evidence(1, 3)

    def test_nested_producers_remain_reusable_after_cleanup(self):
        workflow = self.work / "workflow.py"
        with workflow.open("a") as stream:
            stream.write(
                "task = empty_task(inputs=[sample0.outputs['copy'], reference.outputs['value0']])\n"
                "target = task.target('compute', inputs=task.inputs, outputs=['nested.txt'])\n"
                "target << shell('cat {left} {right} > nested.txt', left=sample0.outputs['copy'], right=reference.outputs['value0'])\n"
                "task.retain('nested', source=target.output('nested.txt'), path='nested.txt')\n"
                "gwf.task(task, alias='nested')\n"
            )
        self.run_complete()
        self.cli("clean-work", "--delete")
        self.assertEqual(self.cli("explain").count("Reuse"), 3)
        self.assertIn("No new jobs were submitted", self.cli("run"))
        self.assertEqual((self.work / "results/nested/nested.txt").read_text(), "hello\nhello\n")

    def test_one_consumer_cannot_reuse_another_consumers_expected_attempt(self):
        self.configure_workflow(consumers=2)
        self.run_complete()
        record = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/sample1/attempts/*/attempt.json"))
        attempt = json.loads(record.read_text())
        attempt["producers"]["reference"] = "0" * 32
        record.write_text(json.dumps(attempt))
        selected = record.parent / "submissions" / attempt["preparation"] / "gwflow_prepare-intent.json"
        intent = json.loads(selected.read_text())
        intent["producers"] = attempt["producers"]
        selected.write_text(json.dumps(intent))
        (record.parent / "admissions" / intent["admission"] / "intent.json").write_text(json.dumps(intent))
        output = self.cli("explain")
        self.assertRegex(output, r"Task sample0\s+Reuse\s+")
        self.assertRegex(output, r"Task sample1\s+Run\s+")
        self.assertNotRegex(output, r"Task sample1\s+Reuse\s+")

    def test_missing_then_incompatible_completion_is_never_reused_and_next_pass_refreshes(self):
        self.run_complete()
        completion = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/sample0/attempts/*/operations/*/completion.json"))
        original = json.loads(completion.read_text())
        completion.unlink()
        first, second = self.scenario([["explain"], ["explain"]], [
            {"pass": 0, "after": "missing:" + str(completion),
             "write": {str(completion): {**original, "attempt": "0" * 32}}},
            {"pass": 0, "after": "planned", "write": {str(completion): original}},
        ])
        self.assertEqual(first["exit_code"], 0)
        self.assertRegex(first["output"], r"Task sample0\s+Blocked\s+")
        self.assertEqual(second["exit_code"], 0)
        self.assertRegex(second["output"], r"Task sample0\s+Reuse\s+")
        self.assertIn("No new jobs were submitted", self.cli("run"))

    def test_reused_metadata_cannot_authorize_a_substituted_symlink(self):
        self.run_complete()
        retained = self.work / "results/reference/value0.txt"
        sentinel = self.work / "outside-sentinel.txt"
        sentinel.write_text("outside data")
        result, = self.scenario([["explain", "--details"]], [{
            "pass": 0, "after": "metadata:" + str(retained),
            "rename": {str(retained): str(retained) + ".saved"},
            "symlink": {str(retained): str(sentinel)},
        }])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("Task sample0", result["output"])
        self.assertIn("State: blocked", result["output"])
        self.assertEqual(sentinel.read_text(), "outside data")

    def test_recorded_cycle_is_rejected_after_prior_producer_validation(self):
        self.run_complete()
        records = self.work / ".gwf/gwflow"
        producer_path = next(records.glob("owners/*/tasks/reference/attempts/*/attempt.json"))
        consumer_path = next(records.glob("owners/*/tasks/sample0/attempts/*/attempt.json"))
        producer = json.loads(producer_path.read_text())
        consumer = json.loads(consumer_path.read_text())
        dependency = {"task": "sample0", "output": "copy"}
        producer["structure"]["inputs"].append(dependency)
        producer["structure"]["targets"]["compute"]["inputs"].append(dependency)
        producer["producers"] = {"sample0": consumer["attempt"]}
        definition = {"structure": producer["structure"],
                      "commands": producer["commands"] if producer["command_tracking"] else None}
        producer["fingerprint"] = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        baseline_path = producer_path.parent / "inputs.json"
        baseline = json.loads(baseline_path.read_text())
        incoming = self.work / "results/sample0/copy.txt"
        info = incoming.stat()
        baseline["inputs"][str(incoming)] = {"resolved": str(incoming), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
        baseline["producers"] = producer["producers"]
        intent_path = producer_path.parent / "submissions" / producer["preparation"] / "gwflow_prepare-intent.json"
        intent = json.loads(intent_path.read_text())
        intent["producers"] = producer["producers"]
        writes = {str(producer_path): producer, str(baseline_path): baseline, str(intent_path): intent,
                  str(producer_path.parent / "admissions" / intent["admission"] / "intent.json"): intent}
        result, = self.scenario([["explain", "--details"]],
                               [{"pass": 0, "after": str(consumer_path), "write": writes}])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("Recorded Task dependency graph contains a cycle", result["output"])
        self.assertIn("State: blocked", result["output"])
        self.assertEqual(incoming.read_text(), "hello\n")
