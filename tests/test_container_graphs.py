"""Mixed execution graphs and image-aware Task dependencies through the CLI."""

from collections import Counter
import json
import os
from pathlib import Path
import shlex
import shutil
import unittest

from support import FIXTURES, LocalBackendTestCase
import test_managed
import test_managed_recovery


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerGraphTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject

    def configure_workflow(self):
        self.first_image = self.work / "first image.sif"
        self.second_image = self.work / "second image.sif"
        for image in (self.first_image, self.second_image):
            shutil.copyfile(os.environ["GWFLOW_TEST_SIF"], image)
        self.write_workflow(consumer=False)

    def write_workflow(self, *, consumer=True, second_image="second image.sif", second_container=True):
        trace = shlex.quote(str(self.work / "trace"))
        source_check = 'test -L {source}; test "$(dirname {source})" = "$PWD"; if echo damaged > {source}; then exit 1; fi; '
        declaration = "from gwflow import Task, Workflow, shell\ngwf = Workflow()\ntask = Task(inputs=[])\n"
        declaration += (
            "host = task.target('host', inputs=[], outputs=['seed.txt'])\n"
            f"host << {'echo host >> ' + trace + '; printf start > seed.txt'!r}\n"
            "first = task.target('first', inputs=[host.output('seed.txt')], outputs=['first.txt'], image='first image.sif')\n"
            f"first << shell({source_check + 'echo first >> ' + trace + '; echo IMAGE=$APPTAINER_CONTAINER; cat {source} > {out}; printf :first >> {out}'!r}, source=host.output('seed.txt'), out=first.output('first.txt'))\n"
            f"second = task.target('second', inputs=[first.output('first.txt')], outputs=['second.txt'], image={second_image if second_container else None!r})\n"
            f"second << shell({(source_check if second_container else '') + 'echo second >> ' + trace + '; echo IMAGE=$APPTAINER_CONTAINER; cat {source} > {out}; printf :second >> {out}'!r}, source=first.output('first.txt'), out=second.output('second.txt'))\n"
            "last = task.target('last', inputs=[second.output('second.txt')], outputs=['final.txt'])\n"
            f"last << shell({'echo last >> ' + trace + '; test ! -L {source}; cat {source} > {out}; touch -m -d @946684800 {out}'!r}, source=second.output('second.txt'), out=last.output('final.txt'))\n"
            "task.retain('value', source=last.output('final.txt'), path='result.txt')\n"
            "producer = gwf.task_from_template('producer', task)\n"
            "task = Task(inputs=[])\n"
            "alone = task.target('compute', inputs=[], outputs=['isolated.txt'])\n"
            f"alone << {'echo independent >> ' + trace + '; printf independent > isolated.txt'!r}\n"
            "task.retain('value', source=alone.output('isolated.txt'), path='result.txt')\n"
            "gwf.task_from_template('independent', task)\n"
        )
        if consumer:
            declaration += (
                "task = Task(inputs=[producer.outputs['value']])\n"
                "read = task.target('read', inputs=task.inputs, outputs=['copy.txt'], image='first image.sif')\n"
                f"read << shell({source_check + 'echo consumer >> ' + trace + '; cat {source} > {out}'!r}, source=producer.outputs['value'], out=read.output('copy.txt'))\n"
                "task.retain('value', source=read.output('copy.txt'), path='result.txt')\n"
                "gwf.task_from_template('consumer', task)\n"
            )
        (self.work / "workflow.py").write_text(declaration)

    def counts(self):
        return Counter((self.work / "trace").read_text().splitlines())

    def detail(self, task, field):
        return next(line.strip().removeprefix(field + ": ")
                    for line in self.cli("explain", task, "--details").splitlines()
                    if line.strip().startswith(field + ": "))

    def attempts(self):
        return {name: self.detail(name, "Attempt") for name in ("producer", "consumer", "independent")}

    def change_image(self):
        info = self.second_image.stat()
        os.utime(self.second_image, ns=(info.st_atime_ns, info.st_mtime_ns - 1000000000))

    def snapshot(self):
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for root in (self.work / ".gwf/gwflow", self.work / "work", self.work / "results")
                for path in root.rglob("*") if path.is_file()}

    def assert_blocked_without_changes(self, reason, *, backend=(), env=None):
        before = self.snapshot()
        for arguments, success in ((("explain", "--details"), True), (("status", "--details"), True),
                                   (("run", "--dry-run"), False), (("run",), False)):
            output = self.cli(*backend, *arguments, env=env, success=success)
            self.assertIn("blocked" if arguments[0] == "status" else reason, output)
            self.assertEqual(self.snapshot(), before)

    def test_host_and_different_container_targets_exchange_checked_outputs(self):
        self.run_complete()
        self.assertEqual((self.work / "results/producer/result.txt").read_text(), "start:first:second")
        for target, image in (("first", self.first_image), ("second", self.second_image)):
            self.assertIn(f"IMAGE={image}", self.cli("logs", f"producer__{target}", "--no-pager"))
        self.assertEqual(self.counts(), {"host": 1, "first": 1, "second": 1, "last": 1, "independent": 1})
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_new_container_consumer_uses_retained_results_after_producer_cleanup(self):
        self.run_complete()
        workspace = Path(self.detail("producer", "Workspace"))
        self.cli("clean-work", "--task", "producer", "--delete")
        self.assertFalse(workspace.exists())
        self.write_workflow()
        output = self.run_complete()
        self.assertEqual(output.count("Submitted target"), 3)
        self.assertEqual((self.work / "results/consumer/result.txt").read_text(), "start:first:second")
        self.assertFalse(workspace.exists())
        self.assertEqual(self.counts(), {"host": 1, "first": 1, "second": 1, "last": 1, "independent": 1, "consumer": 1})
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_image_change_refreshes_whole_task_and_consumers_with_equal_retained_metadata(self):
        self.configure(use_spec_hashes=False)
        self.write_workflow()
        self.run_complete()
        before = self.attempts()
        result = self.work / "results/producer/result.txt"
        info = result.stat()
        self.change_image()
        for arguments in (("explain", "--details"), ("status", "--details"), ("run", "--dry-run", "--details")):
            preview = self.cli(*arguments)
            if arguments[0] == "status":
                self.assertRegex(preview, r"Task producer\s+pending\s+0/6\s+fresh attempt required")
                self.assertRegex(preview, r"Task consumer\s+pending\s+0/3\s+fresh attempt required")
                self.assertRegex(preview, r"Task independent\s+reusable")
            else:
                self.assertIn("input metadata changed", preview)
                self.assertRegex(preview, r"Task producer\s+Run\s+")
                self.assertRegex(preview, r"Task consumer\s+Run\s+")
                self.assertRegex(preview, r"Task independent\s+Reuse\s+")
            self.assertEqual(result.read_text(), "start:first:second")
            self.assertEqual(self.attempts(), before)
        self.run_complete()
        after = self.attempts()
        self.assertNotEqual(after["producer"], before["producer"])
        self.assertNotEqual(after["consumer"], before["consumer"])
        self.assertEqual(after["independent"], before["independent"])
        self.assertEqual((result.stat().st_size, result.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))
        self.assertEqual(self.counts(), {"host": 2, "first": 2, "second": 2, "last": 2, "independent": 1, "consumer": 2})
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_changed_image_alias_and_adding_or_removing_image_refreshes(self):
        self.configure(use_spec_hashes=True)
        self.write_workflow()
        self.run_complete()
        before = self.attempts()
        (self.work / "alias.sif").symlink_to(self.second_image.name)
        self.write_workflow(second_image="alias.sif")
        self.assertIn("changed declared structure", self.cli("explain"))
        self.run_complete()
        after = self.attempts()
        self.assertNotEqual(after["producer"], before["producer"])
        self.assertNotEqual(after["consumer"], before["consumer"])
        self.assertEqual(after["independent"], before["independent"])
        before = after
        self.configure(use_spec_hashes=False)
        for container in (False, True):
            with self.subTest(container=container):
                self.write_workflow(second_container=container)
                self.assertIn("changed declared structure", self.cli("explain"))
                self.run_complete()
                after = self.attempts()
                self.assertNotEqual(after["producer"], before["producer"])
                self.assertNotEqual(after["consumer"], before["consumer"])
                self.assertEqual(after["independent"], before["independent"])
                before = after
        self.assertEqual(self.counts(), {"host": 4, "first": 4, "second": 4, "last": 4, "independent": 1, "consumer": 4})

    def test_image_change_respects_active_jobs_and_removed_active_consumers(self):
        self.write_workflow()
        self.run_complete()
        for active, reason in (("producer", "work blocks replacement"),
                               ("consumer", "Active consumers block replacement: consumer")):
            with self.subTest(active=active):
                if active == "consumer":
                    self.write_workflow(consumer=False)
                self.change_image()
                self.assert_blocked_without_changes(reason, backend=("-b", "recovery_fixture"),
                                                    env=self.inject(queued_prefix=active))

    def test_image_change_cannot_replace_an_uncertain_attempt(self):
        self.write_workflow()
        self.run_complete()
        self.cli("-b", "recovery_fixture", "run", "--force-task", "producer",
                 env=self.inject(reject_prefix="producer__gwflow_prepare"), success=False)
        self.change_image()
        self.assert_blocked_without_changes("unresolved submission")
        self.assertEqual((self.work / "results/independent/result.txt").read_text(), "independent")

    def test_image_change_cannot_remove_results_with_damaged_ownership(self):
        self.write_workflow()
        self.run_complete()
        manifest = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/producer/attempts/*/**/manifest.json"))
        manifest.write_text("{truncated")
        self.change_image()
        self.assert_blocked_without_changes("ownership")

    def test_container_consumer_rechecks_producer_completion_before_staging(self):
        self.write_workflow()
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_before_preparation": True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="consumer__read"))
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        record = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/producer/attempts/*/**/completion.json"))
        saved = record.read_bytes()
        wrong = json.loads(saved)
        wrong["attempt"] = "0" * 32
        record.write_text(json.dumps(wrong))
        (self.work / "preparation-release").touch()
        self.settle()
        self.assertNotIn("consumer", self.counts())
        self.assertIn("Expected producer producer", self.cli("logs", "consumer__read", "--stderr", "--no-pager"))
        self.assertEqual((self.work / "results/producer/result.txt").read_text(), "start:first:second")
        record.write_bytes(saved)
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/consumer/result.txt").read_text(), "start:first:second")
        self.assertEqual(self.counts(), {"host": 1, "first": 1, "second": 1, "last": 1, "independent": 1, "consumer": 1})

    def test_container_references_keep_declaration_ownership_and_cycle_checks(self):
        self.write_workflow()
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        variants = {
            "undeclared target input": original.replace("inputs=[host.output('seed.txt')]", "inputs=[]"),
            "foreign target": original.replace("host = task.target", "host = Task(inputs=[]).target"),
            "target cycle": original.replace("producer = gwf.task_from_template", "host.inputs = [second.output('second.txt')]\nproducer = gwf.task_from_template"),
            "internal cross-Task output": original.replace("producer.outputs['value']", "last.output('final.txt')"),
            "undeclared boundary": original.replace("Task(inputs=[producer.outputs['value']])", "Task(inputs=[])"),
            "foreign Workflow": original.replace("producer = gwf.task_from_template", "producer = Workflow().task_from_template"),
            "Task cycle": original.replace("task = Task(inputs=[producer.outputs['value']])", "cycle = type(producer.outputs['value'])(gwf, 'consumer', 'value')\ntask = Task(inputs=[cycle])").replace("source=producer.outputs['value']", "source=cycle"),
        }
        for label, declaration in variants.items():
            with self.subTest(label=label):
                workflow.write_text(declaration)
                self.cli("run", success=False)
                self.assertFalse((self.work / ".gwf/gwflow").exists())
