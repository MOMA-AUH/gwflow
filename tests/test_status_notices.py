"""Required workflow actions remain visible through every status selection."""

import re

from support import LocalBackendTestCase
import test_fresh
import test_graphs
import test_inspection


class StatusNoticeTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def append_workflow(self, source):
        path = self.work / "workflow.py"
        path.write_text(path.read_text() + source)

    def notice_workflow(self):
        self.append_workflow(
            "for index in range(6):\n"
            "    dependent = empty_task(inputs=[a.outputs['value']])\n"
            "    output = dependent.target('compute', inputs=dependent.inputs, outputs=['out.txt'])\n"
            "    output << 'printf result > out.txt'\n"
            "    dependent.retain('value', source=output.output('out.txt'), path='out.txt')\n"
            "    gwf.task(dependent, alias='deferred', key=str(index))\n"
            "failed = empty_task(inputs=[])\n"
            "left = failed.target('left', inputs=[], outputs=['left.txt'])\n"
            "left << 'printf result > left.txt'\n"
            "right = failed.target('right', inputs=[], outputs=['right.txt'])\n"
            "right << 'exit 8'\n"
            "failed.retain('value', source=left.output('left.txt'), path='out.txt')\n"
            "gwf.task(failed, alias='failure')\n"
        )
        self.cli("run")
        self.settle()
        (self.work / "results/a/result.txt").unlink()
        self.blockers = [f"blocked__{index}_" + "x" * 60 for index in range(7)]
        for name in self.blockers:
            self.append_workflow(
                "blocked = empty_task(inputs=[])\n"
                "output = blocked.target('compute', inputs=[], outputs=['out.txt'], image='missing.sif')\n"
                "output << 'printf result > out.txt'\n"
                "blocked.retain('value', source=output.output('out.txt'), path='out.txt')\n"
                f"gwf.task(blocked, alias={name!r})\n"
            )
        self.deferred = ["c", *(f"deferred__{index}" for index in range(6))]

    def test_excluded_notices_are_uncapped_across_views_and_empty_selections(self):
        self.notice_workflow()
        environment = self.inject(running_prefix="failure__left", record_status=True)
        record = self.work / "backend-observations.jsonl"
        before = self.snapshot()
        baseline = None
        for selectors, count in ((("task:failure*",), 1), (("absent*",), 0),
                                 (("task:failure", "--status", "reusable"), 0)):
            for flags in ((), ("--instances",), ("--details",), ("--instances", "--details")):
                with self.subTest(selectors=selectors, flags=flags):
                    record.unlink(missing_ok=True)
                    output = self.cli("-b", "recovery_fixture", "status", *selectors, *flags, env=environment)
                    self.assertIn(f"{count} of 17 Tasks selected", output)
                    if not count:
                        self.assertIn("No Tasks selected", output)
                    elif flags:
                        self.assertIn("1 job still running", output)
                    notice = output.split("Workflow notices", 1)[1]
                    self.assertIn("no new jobs will be submitted", notice)
                    self.assertIn("Already active jobs may still be running", notice)
                    for name in self.blockers:
                        self.assertIn(name + " (outside selection)", notice)
                    for name in self.deferred:
                        self.assertIn(f"Task {name} (outside selection): run again after upstream result recovery", notice)
                    self.assertEqual(notice.count("(outside selection)"), 14)
                    observed = record.read_text().splitlines()
                    if baseline is None:
                        baseline = observed
                    self.assertEqual(observed, baseline)
                    self.assertEqual(self.snapshot(), before)
        output = self.cli("status", "task:" + self.blockers[0], "--instances")
        notice = output.split("Workflow notices", 1)[1]
        self.assertIn(self.blockers[0] + "\n", notice)
        self.assertNotIn(self.blockers[0] + " (outside selection)", notice)
        self.assertEqual(notice.count("(outside selection)"), 13)

    def test_notices_preserve_all_content_in_narrow_and_plain_output(self):
        self.notice_workflow()
        for flags in ((), ("--instances",), ("--details",)):
            for options in ((), ("--plain",)):
                output = self.terminal_cli("--no-color", "status", "absent*", *flags, *options, width=40).stdout
                compact = re.sub(r"[\s│]", "", output.split("Workflow notices", 1)[1])
                for name in self.blockers:
                    self.assertIn(name + "(outsideselection)", compact)
                for name in self.deferred:
                    self.assertIn(f"Task{name}(outsideselection):runagainafterupstreamresultrecovery", compact)
                self.assertIn("nonewjobswillbesubmitted", compact)

    def test_queued_completion_reminder_uses_existing_retry_condition(self):
        test_graphs.TaskGraphTests.configure_workflow(self, right_command="exit 8")
        self.cli("run")
        self.settle()
        reminder = "run again after the queued completion job settles"
        self.assertNotIn(reminder, self.cli("status"))
        environment = self.inject(queued_prefix="sample__gwflow_complete", record_status=True)
        record = self.work / "backend-observations.jsonl"
        before = self.snapshot()
        baseline = None
        for selection in ("sample*", "absent*"):
            for flags in ((), ("--instances",), ("--details",)):
                record.unlink(missing_ok=True)
                output = self.cli("-b", "recovery_fixture", "status", selection, *flags, env=environment)
                notice = output.split("Workflow notices", 1)[1]
                self.assertIn(reminder, notice)
                self.assertEqual("sample (outside selection)" in notice, selection == "absent*")
                observed = record.read_text().splitlines()
                if baseline is None:
                    baseline = observed
                self.assertEqual(observed, baseline)
                self.assertEqual(self.snapshot(), before)
        output = self.terminal_cli("--no-color", "-b", "recovery_fixture", "status", "absent*",
                                   width=40, env=environment).stdout
        self.assertIn(reminder, " ".join(output.split()))
        output = self.cli("-b", "recovery_fixture", "status", "absent*",
                          env=self.inject(running_prefix="sample__gwflow_complete"))
        self.assertIn("Workflow blocked", output)
        self.assertNotIn(reminder, output)

    def test_routine_and_active_completion_states_do_not_create_notices(self):
        self.assertNotIn("Workflow notices", self.cli("status"))
        self.run_complete()
        self.assertNotIn("Workflow notices", self.cli("status"))
        environment = self.inject(queued_prefix="b__gwflow_complete")
        output = self.cli("-b", "recovery_fixture", "status", "absent*", env=environment)
        self.assertNotIn("Workflow notices", output)
        self.cli("clean-work", "--delete")
        self.assertNotIn("Workflow notices", self.cli("status"))
