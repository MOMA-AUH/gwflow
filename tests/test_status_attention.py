"""Bounded attention and executable inspection commands at the CLI boundary."""

import json
import re
import shlex
import shutil
import time

from support import FIXTURES, LocalBackendTestCase
import test_fresh
import test_inspection


class StatusAttentionTests(LocalBackendTestCase):
    workers = 8
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def wait_for(self, predicate):
        # The wide example executes 270 real jobs before freezing observations.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for attention fixture jobs")

    def configure_workflow(self):
        (self.work / "library.py").write_text(
            "from gwflow import Task, task_template\n"
            "@task_template\n"
            "def work(failures=0, group=None, inputs=()):\n"
            "    task = Task(inputs=inputs)\n"
            "    for index, name in enumerate(('one', 'two', 'three', 'four')):\n"
            "        target = task.target(name, inputs=inputs, outputs=['out.txt'], group=group)\n"
            "        target << ('exit 8' if index < failures else 'printf result > out.txt')\n"
            "    task.retain('value', source=task.targets['two'].output('out.txt'), path='out.txt')\n"
            "    return task\n"
        )
        self.declarations([])

    def declarations(self, entries):
        (self.work / "workflow.py").write_text(
            "from gwflow import Workflow\nfrom library import work\ngwf = Workflow()\n"
            f"for alias, key, failures, group in {entries!r}:\n"
            "    definition = work(failures, group)\n"
            "    gwf.task(definition, alias=alias, key=key)\n"
        )

    def category(self, output, state):
        section = output.split(state.title() + ": ", 1)[1]
        return section.split("All " + state + " Tasks:", 1)[0]

    def run_faults(self, faults):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"jobs": faults}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault_prefixes=list(faults)))
        self.settle()

    def commands(self, output):
        return {state: shlex.split(command) for state, command in re.findall(
            r"All (blocked|failed|canceled) Tasks:\n  (gwf [^\n]+)", output)}

    def names(self, output):
        return re.findall(r"(?m)^Task (\S+)\s+", output)

    def test_independent_caps_include_omitted_and_removed_job_observations(self):
        self.declarations([(alias, str(index), int(alias == 'failed' and index == 5), None)
                           for alias in ('blocked', 'failed', 'canceled') for index in range(6)])
        faults = {f"{alias}__{index}__gwflow_complete": {"crash_before_manifest": True}
                  for alias, count in (("blocked", 6), ("failed", 5)) for index in range(count)}
        faults.update({f"canceled__{index}__{local}": {"crash_before_execution": True}
                       for index in range(6) for local in ("one", "gwflow_complete")})
        self.run_faults(faults)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "    gwf.task", "    if alias == 'blocked': del definition.targets['four']\n    gwf.task") +
            "definition = work()\ndefinition.targets['one'].image = 'missing.sif'\n"
            "gwf.task(definition, alias='blocked', key='image')\n")
        states = {}
        for index in range(6):
            states[f"blocked__{index}__four"] = "RUNNING"
            states[f"canceled__{index}__one"] = "CANCELLED"
            states[f"canceled__{index}__three"] = "RUNNING"
            states[f"canceled__{index}__gwflow_complete"] = "SUBMITTED"
        states["failed__5__three"] = "RUNNING"
        states["failed__5__four"] = "SUBMITTED"
        environment = self.inject(job_states=states, record_status=True)
        before = self.snapshot()
        record = self.work / "backend-observations.jsonl"
        output = self.cli("-b", "recovery_fixture", "status", env=environment)
        baseline = record.read_text().splitlines()
        self.assertIn("19 of 19 Tasks selected", output)
        self.assertIn("7 blocked, 6 failed, 6 canceled", output)
        for state, count in (("blocked", 7), ("failed", 6), ("canceled", 6)):
            section = self.category(output, state)
            self.assertIn(f"{count} Tasks (5 shown, {count - 5} omitted)", section)
            self.assertEqual(re.findall(rf"(?m)^  ({state}__\w+)", section),
                             [f"{state}__{index}" for index in range(5)])
        self.assertIn("Jobs: 6 failed, 6 running", self.category(output, "blocked"))
        self.assertIn("Observations incomplete for 1 Task", self.category(output, "blocked"))
        self.assertIn("Jobs: 7 failed, 1 running, 1 queued", self.category(output, "failed"))
        self.assertIn("Jobs: 6 canceled, 6 running, 6 queued", self.category(output, "canceled"))
        notices = output.split("Workflow notices", 1)[1]
        for name in [*(f"blocked__{index}" for index in range(6)), "blocked__image"]:
            self.assertIn(name, notices)
        for state, command in self.commands(output).items():
            record.unlink()
            expanded = self.cli(*command[1:], env=environment)
            expected = [f"{state}__{index}" for index in range(6)]
            if state == "blocked":
                expected.append("blocked__image")
                self.assertRegex(expanded, r"Task blocked__0\s+blocked\s+0/5")
            self.assertEqual(self.names(expanded), expected)
            self.assertNotIn("Needs attention", expanded)
            self.assertEqual(record.read_text().splitlines(), baseline)
        for flag in ("--instances", "--details"):
            record.unlink()
            expanded = self.cli("-b", "recovery_fixture", "status", flag, env=environment)
            self.assertEqual(len(self.names(expanded)), 19)
            self.assertNotIn("Needs attention", expanded)
            self.assertEqual(record.read_text().splitlines(), baseline)
        self.assertEqual(self.snapshot(), before)

    def test_wide_example_counts_tasks_separately_from_all_observed_jobs(self):
        entries = [("prepare_reference", None, 0, None)]
        entries += [("duplex_mapping", f"sample_{index:02}", int(index == 12), None) for index in range(24)]
        entries += [("mutect2_calling", f"sample_{index:02}", 0, None) for index in range(20)]
        self.declarations(entries)
        self.run_faults({f"duplex_mapping__sample_{index:02}__gwflow_complete": {"crash_before_manifest": True}
                         for index in range(7, 12)})
        states = {f"duplex_mapping__sample_{index:02}__one": "RUNNING" for index in range(4)}
        states.update({f"mutect2_calling__sample_{index:02}__one": "SUBMITTED" for index in range(2)})
        states.update({"duplex_mapping__sample_07__three": "RUNNING",
                       "duplex_mapping__sample_09__four": "SUBMITTED",
                       "duplex_mapping__sample_12__three": "RUNNING",
                       "duplex_mapping__sample_12__four": "RUNNING"})
        environment = self.inject(job_states=states)
        output = self.cli("-b", "recovery_fixture", "status", env=environment)
        self.assertIn("45 of 45 Tasks selected", output)
        self.assertIn("33 reusable, 4 active, 6 failed, 2 queued", output)
        self.assertRegex(output, r"(?m)^prepare_reference\s+1/1$")
        self.assertRegex(output, r"(?m)^duplex_mapping\s+14/24\s+4 active, 6 failed$")
        self.assertRegex(output, r"(?m)^mutect2_calling\s+18/20\s+2 queued$")
        failed = self.category(output, "failed")
        self.assertIn("6 Tasks (5 shown, 1 omitted)", failed)
        self.assertIn("Jobs: 7 failed, 3 running, 1 queued", failed)
        self.assertEqual(re.findall(r"(?m)^  (duplex_mapping__\w+)", failed),
                         [f"duplex_mapping__sample_{index:02}" for index in range(7, 12)])
        command = self.commands(output)["failed"]
        expanded = self.cli(*command[1:], env=environment)
        self.assertEqual(self.names(expanded), [f"duplex_mapping__sample_{index:02}" for index in range(7, 13)])

    def test_inspect_commands_preserve_workflow_context_and_filter_intersection(self):
        self.declarations([(alias, str(index), 0,
                            "selected group" if index % 2 == 0 else "other")
                           for alias in ("failed", "canceled") for index in range(6)])
        faults = {f"failed__{index}__gwflow_complete": {"crash_before_manifest": True} for index in range(6)}
        faults.update({f"canceled__{index}__{local}": {"crash_before_execution": True}
                       for index in range(6) for local in ("one", "gwflow_complete")})
        self.run_faults(faults)
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
            "from gwflow.workflow import RetainedOutput\n"
            "gwf.task(work(inputs=[RetainedOutput(gwf, 'failed__0', 'value')]), alias='consumer')\n"
            "pipeline = gwf\n")
        workflow.rename(self.work / "selected workflow.py")
        states = {f"canceled__{index}__one": "CANCELLED" for index in range(6)}
        states.update({f"canceled__{index}__three": "RUNNING" for index in range(6)})
        states.update({f"canceled__{index}__gwflow_complete": "SUBMITTED" for index in range(6)})
        environment = self.inject(job_states=states)
        selectors = ("task:failed__[024]", "job:canceled__0__one")
        output = self.cli("--file", "selected workflow.py:pipeline", "--backend", "recovery_fixture",
                          "--verbose", "warning", "--no-color", "status", *selectors,
                          "--endpoints", "--group", "selected group", "--status", "failed",
                          "--status", "canceled", env=environment)
        self.assertIn("3 of 13 Tasks selected", output)
        commands = self.commands(output)
        self.assertEqual(set(commands), {"failed", "canceled"})
        for state, command in commands.items():
            self.assertIn("selected workflow.py:pipeline", command)
            self.assertIn("recovery_fixture", command)
            self.assertIn("--no-color", command)
            self.assertIn("--endpoints", command)
            self.assertIn("selected group", command)
            self.assertIn(selectors[0], command)
            self.assertIn(selectors[1], command)
            self.assertEqual(command.count("--status"), 1)
            self.assertEqual(command[command.index("--status") + 1], state)
            expanded = self.cli(*command[1:], env=environment)
            self.assertEqual(self.names(expanded), ["failed__2", "failed__4"] if state == "failed" else ["canceled__0"])

    def test_unavailable_observations_do_not_claim_zero_jobs_or_invent_activity(self):
        self.declarations([("mapping", "A", 0, None)])
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "    gwf.task", "    definition.targets['one'].image = 'missing.sif'\n    gwf.task"))
        output = self.cli("status", "task:mapping__[AB]")
        blocked = self.category(output, "blocked")
        self.assertIn("1 Task", blocked)
        self.assertIn("observations incomplete", blocked.lower())
        self.assertIn("progress unavailable", blocked)
        self.assertNotRegex(blocked, r"\d+ (?:running|queued|failed|canceled)")
        command = self.commands(output)["blocked"]
        self.assertEqual(command, ["gwf", "status", "task:mapping__[AB]", "--status", "blocked", "--instances"])
        expanded = self.cli(*command[1:])
        self.assertRegex(expanded, r"Task mapping__A\s+blocked\s+\?/6")
        for options in ((), ("--plain",)):
            output = self.terminal_cli("--no-color", "status", "task:mapping__[AB]", *options, width=40).stdout
            command = self.commands(output)["blocked"]
            self.assertIn("task:mapping__[AB]", command)
            self.assertEqual(self.names(self.cli(*command[1:])), ["mapping__A"])
