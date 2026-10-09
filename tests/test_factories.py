"""Decorated Task factories through the public API and installed CLI."""

import unittest
import shutil

from gwflow import Task, Workflow
from gwf.exceptions import WorkflowError

from support import LocalBackendTestCase


class TaskFactoryTests(unittest.TestCase):
    def test_default_alias_and_key_names_are_stable(self):
        from gwflow import task_template

        @task_template
        def duplex_mapping():
            task = Task(inputs=[])
            target = task.target("map", inputs=[], outputs=["out.txt"])
            target << "touch out.txt"
            task.retain("result", source=target.output("out.txt"), path="out.txt")
            return task

        workflow = Workflow(working_dir=".")
        handles = [workflow.task(duplex_mapping(), **options) for options in (
            {"key": "sample_A"}, {}, {"alias": "remapping", "key": "sample_B"},
        )]
        self.assertEqual([handle.outputs["result"].task_name for handle in handles],
                         ["duplex_mapping__sample_A", "duplex_mapping", "remapping__sample_B"])
        self.assertIn("duplex_mapping__sample_A__map", workflow.targets)
        self.assertFalse(hasattr(workflow, "task_from_template"))

    def test_registration_requires_decorated_task_and_strict_name_components(self):
        from gwflow import task_template

        @task_template
        def mapping():
            return Task(inputs=[])

        for definition in (Task(inputs=[]), None, object(), "task"):
            with self.subTest(definition=definition), self.assertRaisesRegex(
                    (TypeError, WorkflowError), "Task definition|decorated"):
                Workflow(working_dir=".").task(definition, alias="valid")

        for field in ("alias", "key"):
            for value in ("", 7, False, "a-b", "a/b", " a", "a ", "a\n", "é"):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                        WorkflowError, field):
                    Workflow(working_dir=".").task(mapping(), **{field: value})
        for prefix in ("1mapping", ".mapping"):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(WorkflowError, "alias"):
                Workflow(working_dir=".").task(mapping(), alias=prefix)
        for key in ("1", ".sample", "A__B", "sample.v1"):
            workflow = Workflow(working_dir=".")
            workflow.task(mapping(), key=key, alias="_Mapping__v1")
            self.assertIn(f"_Mapping__v1__{key}__gwflow_prepare", workflow.targets)

        @task_template
        def invalid():
            return "not a Task"

        with self.assertRaisesRegex(TypeError, "Task definition"):
            invalid()

    def test_collisions_identify_declarations_and_namespace_overlap_is_allowed(self):
        from gwflow import task_template

        @task_template
        def first(local="write"):
            task = Task(inputs=[])
            task.target(local, inputs=[], outputs=["out"])
            return task

        @task_template
        def second(local="write"):
            return first(local)

        workflow = Workflow(working_dir=".")
        workflow.task(first("b__write"), alias="a")
        with self.assertRaisesRegex(WorkflowError, "a__b__write.*first.*second"):
            workflow.task(second(), alias="a__b")
        with self.assertRaisesRegex(WorkflowError, "a.*first.*second"):
            workflow.task(second(), alias="a")
        workflow.task(second(), alias="a__b__write")
        self.assertIn("a__b__write__write", workflow.targets)

    def test_factory_calls_and_registrations_are_independent(self):
        from gwflow import task_template

        shared = Task(inputs=[])
        shared.target("write", inputs=[], outputs=["out"])

        @task_template
        def cached():
            return shared

        one, two = cached(), cached()
        one.target("extra", inputs=[], outputs=["extra"])
        self.assertEqual(set(two.targets), {"write"})
        self.assertEqual(set(shared.targets), {"write"})
        workflow = Workflow(working_dir=".")
        workflow.task(one, key="1")
        one.target("late", inputs=[], outputs=["late"])
        workflow.task(one, key="2")
        self.assertNotIn("cached__1__late", workflow.targets)
        self.assertIn("cached__2__late", workflow.targets)


class FactoryReuseTests(LocalBackendTestCase):
    def configure_workflow(self):
        (self.work / "library.py").write_text(
            "from gwflow import Task, shell, task_template\n"
            "@task_template\n"
            "def producer():\n"
            "    task = Task(inputs=['input.txt'])\n"
            "    target = task.target('copy', inputs=task.inputs, outputs=['out.txt'])\n"
            "    target << shell('cat {source} > out.txt', source='input.txt')\n"
            "    task.retain('value', source=target.output('out.txt'), path='out.txt')\n"
            "    return task\n"
        )
        (self.work / "workflow.py").write_text(
            "from gwflow import Workflow\n"
            "from library import producer\n"
            "gwf = Workflow()\n"
            "definition = producer()\n"
            "gwf.task(definition, key='A')\n"
            "gwf.task(definition, key='B')\n"
        )

    def test_module_move_reordering_and_cleanup_preserve_reuse_but_new_name_cannot_adopt(self):
        self.run_complete()
        self.cli("clean-work", "--delete")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for root in (self.work / ".gwf/gwflow", self.work / "results")
                  for p in root.rglob("*") if p.is_file()}
        (self.work / "library.py").rename(self.work / "moved.py")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("from library", "from moved").replace(
            "gwf.task(definition, key='A')\ngwf.task(definition, key='B')",
            "gwf.task(definition, key='B')\ngwf.task(definition, key='A')"))
        self.assertEqual(self.cli("explain").count("Reuse"), 2)
        self.assertNotIn("Submitted target", self.cli("run"))
        for path, evidence in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), evidence)
        workflow.write_text(workflow.read_text().replace("gwf.task(definition, key='A')",
                                                       "gwf.task(definition, key='C', result_dir='producer__A')"))
        self.assertIn("existing files are not adopted", self.cli("run", success=False))
        self.assertEqual((self.work / "results/producer__A/out.txt").read_text(), "hello\n")

    def test_nested_and_same_named_factories_keep_distinct_names_and_work(self):
        shutil.copy(self.work / "library.py", self.work / "second.py")
        workflow = self.work / "workflow.py"
        workflow.write_text(
            "from gwflow import Workflow, task_template\n"
            "from library import producer\n"
            "from second import producer as second\n"
            "@task_template\n"
            "def outer():\n"
            "    return producer()\n"
            "gwf = Workflow()\n"
            "gwf.task(producer(), key='A')\n"
            "gwf.task(second(), key='B')\n"
            "gwf.task(outer())\n"
        )
        self.run_complete()
        for name in ("producer__A", "producer__B", "outer"):
            self.assertEqual((self.work / "results" / name / "out.txt").read_text(), "hello\n")
