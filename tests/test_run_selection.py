"""Selected execution through the installed CLI and real local workers."""

from collections import Counter
import re
import shlex

from support import TASK_FACTORY, LocalBackendTestCase
from test_managed_recovery import ManagedCoordinationTests
from test_registry_images import RegistryTestCase


class SelectedRunTests(LocalBackendTestCase):
    inject = ManagedCoordinationTests.inject

    def configure_workflow(self, tasks=(("a", ()), ("b", ("a",)), ("c", ("b", "a")), ("d", ()), ("e", ("c",)))):
        trace = shlex.quote(str(self.work / "trace.txt"))
        declaration = TASK_FACTORY + "from gwflow import Workflow, shell\ngwf = Workflow()\nhandles = {}\n"
        for name, producers in tasks:
            inputs = ", ".join(f"handles[{producer!r}].outputs['value']" for producer in producers)
            sources = " ".join("{source" + str(index) + "}" for index in range(len(producers)))
            bindings = ", ".join(f"source{index}=handles[{producer!r}].outputs['value']"
                                 for index, producer in enumerate(producers))
            command = (f"echo {name} >> {trace}; "
                       + (f"cat {sources} > {{out}}; printf {name} >> {{out}}" if producers
                          else f"printf {name} > {{out}}"))
            declaration += (
                f"task = empty_task(inputs=[{inputs}])\n"
                "target = task.target('compute', inputs=task.inputs, outputs=['value.txt'])\n"
                f"target << shell({command!r}, out=target.output('value.txt')"
                + (f", {bindings}" if bindings else "") + ")\n"
                "task.retain('value', source=target.output('value.txt'), path='value.txt')\n"
                f"handles[{name!r}] = gwf.task(task, alias={name!r})\n"
            )
        (self.work / "workflow.py").write_text(declaration)

    def actions(self, output):
        return dict(re.findall(r"(?m)^Task ([a-z][a-z0-9_]*)\s+(\w+)", output))

    def task_evidence(self, *names):
        roots = [root for name in names for root in (
            self.work / "work" / name, self.work / "results" / name,
            *(self.work / ".gwf/gwflow").glob(f"owners/*/tasks/{name}"))]
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for root in roots for path in root.rglob("*") if path.is_file()}

    def test_selected_consumer_runs_shared_upstream_once_and_excludes_other_tasks(self):
        preview = self.cli("run", "c", "--dry-run", "--details")
        self.assertEqual(self.actions(preview), {"a": "Run", "b": "Run", "c": "Run"})
        self.assertFalse((self.work / "work").exists())
        self.assertFalse((self.work / "results").exists())
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        actual = self.cli("run", "c", "--details")
        self.assertEqual(self.actions(actual), self.actions(preview))
        self.assertEqual(actual.count("Submitted target "), 9)
        self.finish()
        self.assertEqual(self.trace(), Counter({"a": 1, "b": 1, "c": 1}))
        self.assertEqual((self.work / "results/c/value.txt").read_text(), "abac")
        for name in ("d", "e"):
            self.assertFalse((self.work / "work" / name).exists())
            self.assertFalse((self.work / "results" / name).exists())
        self.assertNotIn("Submitted target", self.cli("run", "c"))

    def test_force_refreshes_selected_tasks_only_and_default_force_refreshes_all(self):
        self.run_complete()
        attempts = self.task_attempts()
        before = self.task_evidence("a", "b", "c", "d", "e")
        outside = self.task_evidence("a", "b", "d", "e")
        preview = self.cli("run", "c", "--force", "--dry-run")
        self.assertEqual(self.actions(preview), {"a": "Reuse", "b": "Reuse", "c": "Run"})
        self.assertEqual(self.task_evidence("a", "b", "c", "d", "e"), before)
        actual = self.cli("run", "c", "--force")
        self.assertEqual(self.actions(actual), self.actions(preview))
        self.assertEqual(actual.count("Submitted target "), 3)
        self.finish()
        self.assertEqual(self.task_evidence("a", "b", "d", "e"), outside)
        current = self.task_attempts()
        self.assertNotEqual(current.pop("c"), attempts.pop("c"))
        self.assertEqual(current, attempts)
        self.assertEqual(self.trace(), Counter({"a": 1, "b": 1, "c": 2, "d": 1, "e": 1}))
        attempts = self.task_attempts()
        self.cli("run", "--force")
        self.finish()
        current = self.task_attempts()
        self.assertTrue(all(current[name] != attempt for name, attempt in attempts.items()))
        self.assertEqual(self.trace(), Counter({"a": 2, "b": 2, "c": 3, "d": 2, "e": 2}))

    def test_globs_multiple_selections_and_default_scope(self):
        for selectors in (("[bc]", "b", "b"), ("b", "c")):
            preview = self.cli("run", "--dry-run", *selectors)
            self.assertEqual(self.actions(preview), {"a": "Run", "b": "Run", "c": "Run"})
        self.cli("run", "[bc]", "b", "b")
        self.finish()
        before = self.task_evidence("a", "b", "c", "e")
        output = self.cli("run", "[cd]", "c")
        self.assertEqual(self.actions(output), {"a": "Reuse", "b": "Reuse", "c": "Reuse", "d": "Run"})
        self.finish()
        self.assertEqual(self.task_evidence("a", "b", "c", "e"), before)
        self.assertEqual(self.trace(), Counter({"a": 1, "b": 1, "c": 1, "d": 1}))
        self.assertEqual(self.actions(self.cli("run", "--dry-run")),
                         {"a": "Reuse", "b": "Reuse", "c": "Reuse", "d": "Reuse", "e": "Run"})
        self.run_complete()
        self.assertEqual(self.trace(), Counter({"a": 1, "b": 1, "c": 1, "d": 1, "e": 1}))

    def test_unmatched_selectors_reject_the_entire_run_and_preview_without_changes(self):
        self.run_complete()
        before = self.task_evidence("a", "b", "c", "d", "e")
        unmatched = ("missing", "absent*", "a__compute", "task:a", "job:a__compute")
        for options in ((), ("--dry-run",)):
            output = self.cli("run", "--force", "a", *unmatched, *options, success=False)
            self.assertIn("Unmatched Task selectors", output)
            for selector in unmatched:
                self.assertIn(repr(selector), output)
            self.assertNotIn("Submitted target", output)
            self.assertEqual(self.task_evidence("a", "b", "c", "d", "e"), before)

    def test_invalid_upstream_is_refreshed_but_cleaned_valid_upstream_is_reused(self):
        self.run_complete()
        self.cli("clean", "--delete", "a", "b")
        self.assertEqual(self.actions(self.cli("run", "c", "--force", "--dry-run")),
                         {"a": "Reuse", "b": "Reuse", "c": "Run"})
        (self.work / "results/a/value.txt").unlink()
        outside = self.task_evidence("d", "e")
        preview = self.cli("run", "c", "--force", "--dry-run")
        self.assertEqual(self.actions(preview), {"a": "Run", "b": "Run", "c": "Run"})
        self.assertEqual(self.actions(self.cli("run", "c", "--force")), self.actions(preview))
        self.finish()
        self.assertEqual(self.task_evidence("d", "e"), outside)
        self.assertEqual((self.work / "results/c/value.txt").read_text(), "abac")
        self.assertEqual(self.trace(), Counter({"a": 2, "b": 2, "c": 2, "d": 1, "e": 1}))

    def test_selected_execution_preserves_shared_root_ownership_checks(self):
        self.run_complete()
        saved = self.work / "owned-work"
        (self.work / "work").rename(saved)
        outside = self.work / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("unowned")
        (self.work / "work").symlink_to(outside, target_is_directory=True)
        before = self.task_evidence("a", "b", "c", "d", "e")
        for options in ((), ("--dry-run",)):
            output = self.cli("run", "c", "--force", *options, success=False)
            self.assertNotIn("Submitted target", output)
            self.assertEqual(self.task_evidence("a", "b", "c", "d", "e"), before)
            self.assertEqual(sentinel.read_text(), "unowned")
            self.assertTrue((self.work / "work").is_symlink())
            self.assertTrue(list(saved.iterdir()))

    def test_unrelated_consumer_observation_failure_does_not_block_selected_execution(self):
        self.configure_workflow(tasks=(("a", ()), ("b", ("a",)), ("d", ())))
        self.run_complete()
        outside = self.task_evidence("a", "b")
        env = self.inject(fail_status_prefix="b")
        preview = self.cli("-b", "recovery_fixture", "run", "d", "--force", "--dry-run", env=env)
        self.assertEqual(self.actions(preview), {"d": "Run"})
        actual = self.cli("-b", "recovery_fixture", "run", "d", "--force", env=env)
        self.assertEqual(self.actions(actual), self.actions(preview))
        self.finish()
        self.assertEqual(self.task_evidence("a", "b"), outside)
        self.assertEqual(self.trace(), Counter({"a": 1, "b": 1, "d": 2}))
        before = self.task_evidence("a", "b", "d")
        for options in ((), ("--dry-run",)):
            output = self.cli("-b", "recovery_fixture", "run", "a", "--force", *options, env=env, success=False)
            self.assertIn("injected backend observation failure", output)
            self.assertNotIn("Submitted target", output)
            self.assertEqual(self.task_evidence("a", "b", "d"), before)

    def test_reusable_upstream_does_not_require_unselected_consumer_observations(self):
        self.configure_workflow(tasks=(("a", ()), ("b", ("a",)), ("d", ("a",))))
        self.run_complete()
        before = self.task_evidence("a", "b")
        env = self.inject(fail_status_prefix="b")
        preview = self.cli("-b", "recovery_fixture", "run", "d", "--force", "--dry-run", env=env)
        self.assertEqual(self.actions(preview), {"a": "Reuse", "d": "Run"})
        actual = self.cli("-b", "recovery_fixture", "run", "d", "--force", env=env)
        self.assertEqual(self.actions(actual), self.actions(preview))
        self.finish()
        self.assertEqual(self.task_evidence("a", "b"), before)
        self.assertEqual(self.trace(), Counter({"a": 1, "b": 1, "d": 2}))


class SelectedImageTests(RegistryTestCase):
    actions = SelectedRunTests.actions

    def configure_workflow(self, reference=None, *, other=True):
        super().configure_workflow(reference, other=other)

    def test_unrelated_image_failure_is_not_acquired_but_selected_failure_blocks_submission(self):
        self.options(fail=True)
        self.assertEqual(self.actions(self.cli("run", "independent", "--dry-run")), {"independent": "Run"})
        self.cli("run", "independent")
        self.finish()
        self.assertEqual(self.calls("pull"), [])
        self.assertEqual((self.work / "results/independent/out.txt").read_text(), "independent")
        before = self.snapshot()
        for options in (("--dry-run",), ()):
            output = self.cli("run", "sample", "independent", "--force", *options, success=False)
            self.assertIn("image unavailable", output)
            self.assertNotIn("Submitted target", output)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual([call["reference"] for call in self.calls("pull")], [self.reference, self.reference])
        self.configure_workflow("missing-local-image.sif")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("empty_task(inputs=[])", "empty_task(inputs=['missing-input.txt'])", 1))
        self.assertEqual(self.actions(self.cli("run", "independent")), {"independent": "Reuse"})
        self.assertEqual(self.snapshot(), before)

    def test_selected_consumer_acquires_upstream_image_even_when_reusable_and_keeps_warm_cache(self):
        workflow = self.work / "workflow.py"
        source = workflow.read_text().replace("gwf.task(task, alias='sample')", "producer = gwf.task(task, alias='sample')")
        source += (
            "from gwflow import shell\n"
            "task = empty_task(inputs=[producer.outputs['result']])\n"
            "target = task.target('copy', inputs=task.inputs, outputs=['copy.txt'])\n"
            "target << shell('cat {source} > copy.txt', source=producer.outputs['result'])\n"
            "task.retain('copy', source=target.output('copy.txt'), path='copy.txt')\n"
            "gwf.task(task, alias='consumer')\n"
        )
        workflow.write_text(source)
        self.assertEqual(self.actions(self.cli("run", "consumer", "--dry-run")), {"sample": "Run", "consumer": "Run"})
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.cli("run", "consumer")
        self.finish()
        self.assertEqual((self.work / "results/consumer/copy.txt").read_bytes(), b"fixture image\n")
        image, = self.cached_images()
        self.options(mtime_ns=image.stat().st_mtime_ns)
        image.unlink()
        before = self.snapshot()
        self.assertEqual(self.actions(self.cli("run", "consumer", "--dry-run")), {"sample": "Reuse", "consumer": "Reuse"})
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("Submitted target", self.cli("run", "consumer"))
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertFalse((self.work / "results/independent").exists())
