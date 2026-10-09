"""Complete identifiers and associated status fields at every terminal width."""

import re
import shlex
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_fresh
import test_grouped_status
import test_inspection


def uncolored(output):
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


class StatusLayoutTests(LocalBackendTestCase):
    configure_workflow = test_grouped_status.GroupedStatusTests.configure_workflow
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def test_full_long_names_commands_and_evidence_across_views_and_controls(self):
        prefix = "mapping_" + "x" * 72
        local = "compute_" + "z" * 60
        library = self.work / "library.py"
        library.write_text(library.read_text().replace("'compute'", repr(local)))
        (self.work / "second.py").write_text(library.read_text())
        workflow = self.work / "workflow.py"
        source = ("from gwflow import Workflow\nfrom library import mapping\n"
                  "from second import mapping as other\ngwf = Workflow()\n"
                  "definition = mapping()\n"
                  f"gwf.task(definition, alias={prefix!r}, key='A')\n"
                  f"gwf.task(other(), alias={prefix!r}, key='B')\n")
        workflow.write_text(source)
        self.run_complete()
        workflow.write_text(source.replace("definition = mapping()", "definition = mapping()\n"
                                          f"definition.targets[{local!r}].image = 'missing.sif'"))
        environment = self.inject(record_status=True)
        record = self.work / "backend-observations.jsonl"
        before = self.snapshot()
        baseline = None
        controls = (("--no-color", ()), ("--use-color", ()),
                    ("--use-color", ("--plain",)), ("--no-color", ("--no-truncate",)))
        selector = "task:" + prefix + "__*"
        for width in (40, 100):
            for view in ((), ("--instances",), ("--details",)):
                for color, options in controls:
                    with self.subTest(width=width, view=view, color=color, options=options):
                        record.unlink(missing_ok=True)
                        raw = self.terminal_cli(color, "-b", "recovery_fixture", "status", selector,
                                                *view, *options, width=width, env=environment).stdout
                        output = uncolored(raw)
                        self.assertIn("2 of 2 Tasks selected", " ".join(output.split()))
                        self.assertNotIn("…", output)
                        self.assertNotRegex(output, "[╭╮╰╯━]")
                        if not view:
                            for module in ("library", "second"):
                                self.assertIn(f"{prefix}@{module}.mapping", output)
                            command = re.search(r"(?m)^  (gwf [^\n]+)$", output).group(1)
                            self.assertIn(selector, shlex.split(command))
                        else:
                            for key in ("A", "B"):
                                self.assertIn(f"{prefix}__{key}", output)
                                if "--details" in view:
                                    for job in (local, "gwflow_prepare", "gwflow_complete"):
                                        self.assertIn(f"{prefix}__{key}__{job}", output)
                        if color == "--no-color" or "--plain" in options:
                            self.assertNotIn("\x1b[", raw)
                        observed = record.read_text().splitlines()
                        if baseline is None:
                            baseline = observed
                        self.assertEqual(observed, baseline)
                        self.assertEqual(self.snapshot(), before)
        redirected = self.cli("--use-color", "-b", "recovery_fixture", "status", "--details", env=environment)
        self.assertIn(f"{prefix}__A__{local}", redirected)
        self.assertNotIn("\x1b[", redirected)
        for view in ((), ("--instances",), ("--details",)):
            output = self.terminal_cli("--no-color", "status", "absent*", *view, width=40).stdout
            self.assertIn("No Tasks selected", output)
            self.assertIn(f"{prefix}__A", output)
            self.assertIn("(outside selection)", output)

    def test_narrow_filtered_example_preserves_progress_notices_and_inspection(self):
        (self.work / "library.py").write_text(
            "from gwflow import Task, shell, task_template\n"
            "@task_template\n"
            "def mapping(inputs=(), fail=False):\n"
            "    task = Task(inputs=inputs)\n"
            "    left = task.target('left', inputs=inputs, outputs=['left.txt'])\n"
            "    left << 'printf left > left.txt'\n"
            "    right = task.target('right', inputs=[], outputs=['right.txt'])\n"
            "    right << ('exit 8' if fail else 'printf right > right.txt')\n"
            "    join = task.target('join', inputs=[left.output('left.txt'), right.output('right.txt')], outputs=['out.txt'])\n"
            "    join << shell('cat {left} {right} > out.txt', left=left.output('left.txt'), right=right.output('right.txt'))\n"
            "    task.retain('value', source=join.output('out.txt'), path='out.txt')\n"
            "    return task\n"
        )
        header = "from gwflow import Workflow\nfrom library import mapping\ngwf = Workflow()\n"
        initial = ("reference = gwf.task(mapping(), alias='prepare_reference')\n"
                   "gwf.task(mapping(fail=True), alias='duplex_mapping', key='sample_B')\n"
                   "gwf.task(mapping([reference.outputs['value']]), alias='mutect2_calling', key='sample_D')\n")
        workflow = self.work / "workflow.py"
        workflow.write_text(header + initial.replace(
            "gwf.task(mapping(fail=True), alias='duplex_mapping', key='sample_B')\n", ""))
        self.run_complete()
        workflow.write_text(header + initial)
        interrupted = subprocess.run(
            [sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work),
             "after_job_ack:right", "duplex_mapping__sample_B", "run"],
            cwd=self.work, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(interrupted.returncode, 111, interrupted.stdout + interrupted.stderr)
        self.settle()
        (self.work / "results/prepare_reference/out.txt").unlink()

        def blocked(alias, key):
            return ("definition = mapping()\ndefinition.targets['left'].image = 'missing.sif'\n"
                    f"gwf.task(definition, alias={alias!r}, key={key!r})\n")

        workflow.write_text(header + blocked("duplex_mapping", "sample_A") +
                            initial.replace("gwf.task(mapping([reference.outputs['value']])",
                                            blocked("mutect2_calling", "sample_C") +
                                            "gwf.task(mapping([reference.outputs['value']])") +
                            "for alias in ('duplex_mapping', 'mutect2_calling'):\n"
                            "    for index in range(20):\n"
                            "        gwf.task(mapping(), alias=alias, key=f'other_{index:02}')\n")
        selector = "task:duplex_mapping__sample_[AB]"
        before = self.snapshot()
        for color, options in (("--no-color", ()), ("--use-color", ()),
                               ("--no-color", ("--plain",)), ("--no-color", ("--no-truncate",))):
            raw = self.terminal_cli(color, "status", selector, "--instances", *options, width=40).stdout
            output = uncolored(raw)
            self.assertIn("2 of 45 Tasks selected\n1 blocked, 1 failed", output)
            self.assertIn("State: blocked\n  Jobs completed: ?/5", output)
            self.assertIn("State: failed\n  Jobs completed: 2/5", output)
            self.assertIn("progress unavailable", " ".join(output.split()))
            self.assertIn("1 job failed", output)
            self.assertNotIn("Needs attention", output)
            self.assertNotRegex(output, "[╭╮╰╯━…]")
            self.assertTrue(all(len(line) <= 40 for line in output.splitlines()), output)
            notices = output.split("Workflow notices", 1)[1]
            self.assertIn("duplex_mapping__sample_A", notices)
            self.assertIn("mutect2_calling__sample_C", notices)
            self.assertIn("mutect2_calling__sample_D", notices)
            self.assertEqual(notices.count("(outside selection)"), 2)
            self.assertIn("no new jobs will be submitted", " ".join(notices.split()))
            self.assertIn("run again after upstream result recovery", " ".join(notices.split()))
            self.assertEqual(self.snapshot(), before)
        grouped = self.terminal_cli("--no-color", "status", selector, width=40).stdout
        commands = re.findall(r"(?m)^  (gwf [^\n]+)$", grouped)
        failed = next(shlex.split(command) for command in commands if "--status failed" in command)
        self.assertIn(selector, failed)
        expanded = self.cli(*failed[1:])
        self.assertIn("1 of 45 Tasks selected", expanded)
        self.assertRegex(expanded, r"Task duplex_mapping__sample_B\s+failed\s+2/5")
        self.assertEqual(self.snapshot(), before)
