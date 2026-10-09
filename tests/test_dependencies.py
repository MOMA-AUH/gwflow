"""Composition through named retained references and ordinary CLI execution."""

from collections import Counter
import json
import shlex
import shutil

from support import TASK_FACTORY, FIXTURES, LocalBackendTestCase
import test_managed
import test_managed_recovery


class TaskDependencyTests(LocalBackendTestCase):
    workers = 4
    slurm_environment = test_managed_recovery.ManagedCoordinationTests.slurm_environment
    inject = test_managed_recovery.ManagedCoordinationTests.inject
    settle = test_managed.ManagedCliTests.settle

    def configure_workflow(self, *, consumer=True):
        trace = shlex.quote(str(self.work / "trace"))
        declaration = (
            TASK_FACTORY + "from gwflow import Task, Workflow, shell\n"
            "gwf = Workflow(results_root='retained data')\n"
            "producer = empty_task(inputs=[])\n"
            "target = producer.target('compute', inputs=[], outputs=['private.txt'])\n"
            f"target << {'echo producer >> ' + trace + '; printf data > private.txt'!r}\n"
            "producer.retain('value', source=target.output('private.txt'), path='nested/public.txt')\n"
            "a = gwf.task(producer, alias='a', result_dir='first producer')\n"
            "b = gwf.task(producer, alias='b', result_dir='second producer')\n"
        )
        if consumer:
            declaration += (
                "consumer = empty_task(inputs=[a.outputs['value'], b.outputs['value']])\n"
                "combine = consumer.target('combine', inputs=consumer.inputs, outputs=['combined.txt'])\n"
                f"combine << shell({'echo consumer >> ' + trace + '; cat {a} {b} > {out}'!r}, a=a.outputs['value'], b=b.outputs['value'], out=combine.output('combined.txt'))\n"
                "consumer.retain('combined', source=combine.output('combined.txt'), path='result.txt')\n"
                "gwf.task(consumer, alias='c')\n"
            )
        (self.work / "workflow.py").write_text(declaration)

    def test_new_consumer_uses_retained_producers_after_work_removal(self):
        self.configure_workflow(consumer=False)
        self.run_complete()
        for name in ("a", "b"):
            shutil.rmtree(self.work / "work" / name)
        self.configure_workflow()
        self.run_complete()
        self.assertEqual((self.work / "retained data/c/result.txt").read_text(), "datadata")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"producer": 2, "consumer": 1})
        for name in ("a", "b"):
            self.assertFalse((self.work / "work" / name).exists())
        self.assertEqual(self.cli("explain").count("Reuse"), 3)
        self.cli("run")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"producer": 2, "consumer": 1})

    def test_slurm_preparation_waits_for_complete_independent_producers(self):
        release = self.work / "release-producers"
        workflow = self.work / "workflow.py"
        trace = shlex.quote(str(self.work / "trace"))
        wait = f"echo tail >> {trace}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf tail > tail.txt"
        workflow.write_text(workflow.read_text().replace("producer.retain(",
            "tail = producer.target('tail', inputs=[], outputs=['tail.txt'])\n" + f"tail << {wait!r}\nproducer.retain("))
        env = self.slurm_environment()
        self.cli("-b", "slurm", "run", env=env)
        try:
            self.wait_for(lambda: (self.work / "trace").exists() and Counter((self.work / "trace").read_text().splitlines())["tail"] == 2)
            records = [json.loads(line) for line in (self.work / "slurm-submitted.jsonl").read_text().splitlines()]
            def submitted(prefix):
                return next(item for item in records if item["name"].startswith(prefix + "__"))
            producer_ids = [submitted(name + "__gwflow_complete")["id"] for name in ("a", "b")]
            self.assertIn("--dependency=afterok:" + ":".join(producer_ids), submitted("c__gwflow_prepare")["args"])
            self.assertNotIn("consumer", (self.work / "trace").read_text().splitlines())
            self.assertFalse((self.work / "retained data/first producer").exists())
            explanation = self.cli("-b", "slurm", "explain", "--details", "a", env=env)
            self.assertIn("Active consumer c:", explanation)
            self.assertIn("c__gwflow_prepare", explanation)
        finally:
            release.touch()
        self.finish()
        self.assertEqual((self.work / "retained data/c/result.txt").read_text(), "datadata")

    def test_invalid_retained_dependencies_fail_before_initialization(self):
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        variants = {
            "unknown public output": original.replace("a.outputs['value']", "a.outputs['private.txt']"),
            "foreign Workflow": original.replace("a = gwf.task", "a = Workflow().task"),
            "internal output": original.replace("a.outputs['value']", "target.output('private.txt')"),
            "undeclared boundary": original.replace("empty_task(inputs=[a.outputs['value'], b.outputs['value']])", "empty_task(inputs=[b.outputs['value']])"),
            "undeclared target input": original.replace("inputs=consumer.inputs", "inputs=[b.outputs['value']]"),
            "Task cycle": original.replace("consumer = empty_task(", "cyclic = type(a.outputs['value'])(gwf, 'c', 'combined')\nconsumer = empty_task(").replace("a.outputs['value'], b.outputs['value']", "cyclic, b.outputs['value']").replace("a=a.outputs['value']", "a=cyclic"),
        }
        for label, declaration in variants.items():
            with self.subTest(label=label):
                workflow.write_text(declaration)
                self.cli("run", success=False)
                self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_active_consumer_is_visible_after_its_declaration_is_removed(self):
        held, release = (self.work / name for name in ("consumer-held", "consumer-release"))
        workflow = self.work / "workflow.py"
        wait = f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
        workflow.write_text(workflow.read_text().replace("echo consumer", wait + "echo consumer"))
        self.cli("run")
        self.wait_for(held.exists)
        try:
            expected = self.cli("explain", "--details", "c")
            self.assertIn("Expected producer a:", expected)
            self.assertIn("Expected producer b:", expected)
            self.configure_workflow(consumer=False)
            self.assertIn("Active consumer c:", self.cli("explain", "--details", "a"))
            self.assertIn("Active consumer c:", self.cli("explain", "--details", "b"))
        finally:
            release.touch()
        self.finish()
        self.assertNotIn("Active consumer c:", self.cli("explain", "--details", "a"))

    def wrong_generation_before(self, job):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_before_preparation": True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="c__" + job))
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        record = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/a/attempts/*/**/completion.json"))
        saved = record.read_bytes()
        wrong = json.loads(saved)
        wrong["attempt"] = "0" * 32
        record.write_text(json.dumps(wrong))
        (self.work / "preparation-release").touch()
        self.settle()
        self.assertNotIn("consumer", (self.work / "trace").read_text().splitlines())
        self.assertEqual((self.work / "retained data/first producer/nested/public.txt").read_text(), "data")
        self.assertIn("Expected producer a", self.cli("logs", "c__" + job, "--stderr", "--no-pager"))
        record.write_bytes(saved)
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "retained data/c/result.txt").read_text(), "datadata")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"producer": 2, "consumer": 1})

    def test_wrong_generation_completion_cannot_supply_preparation(self):
        self.wrong_generation_before("gwflow_prepare")

    def test_computation_rechecks_producer_completion_after_preparation(self):
        self.wrong_generation_before("combine")

    def test_consumer_finishing_still_protects_bound_producers(self):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps({"gate_after_manifest": True}))
        self.cli("-b", "recovery_fixture", "run", env=self.inject(job_fault="c__gwflow_complete"))
        self.wait_for(lambda: (self.work / "manifest-held").exists())
        try:
            self.assertIn("consumer", (self.work / "trace").read_text().splitlines())
            self.assertFalse((self.work / "retained data/c").exists())
            explanation = self.cli("explain", "--details", "a")
            self.assertIn("Active consumer c:", explanation)
            self.assertIn("c__gwflow_complete", explanation)
        finally:
            (self.work / "manifest-release").touch()
        self.finish()
        self.assertNotIn("Active consumer c:", self.cli("explain", "--details", "a"))
