"""Selected status observes upstream dependencies without displaying them as selected."""

import re
import shlex

from support import LocalBackendTestCase
import test_run_selection
import test_registry_images


class StatusScopeTests(LocalBackendTestCase):
    configure_workflow = test_run_selection.SelectedRunTests.configure_workflow
    task_evidence = test_run_selection.SelectedRunTests.task_evidence

    def names(self, output):
        return re.findall(r"(?m)^(\S+)[ \t]+\w+[ \t]+[?\d]+/\d+", output)

    def test_selected_details_explain_upstream_blockers_without_unrelated_tasks(self):
        workflow = self.work / "workflow.py"
        source = workflow.read_text().replace("handles['a'] =", "target.image = 'missing-upstream.sif'\nhandles['a'] =")
        source = source.replace("handles['d'] =", "target.image = 'unrelated.sif'\nhandles['d'] =")
        workflow.write_text(source)
        before = self.task_evidence("a", "b", "c", "d", "e")
        output = self.cli("status", "c", "--details")
        self.assertEqual(self.names(output), ["c"])
        self.assertIn("1 of 5 Tasks selected", output)
        self.assertIn("a (outside selection)", output)
        self.assertIn("image unavailable", output)
        self.assertNotIn("unrelated.sif", output)
        self.assertNotRegex(output, r"(?m)^d\s")
        self.assertNotRegex(output, r"(?m)^e\s")
        self.assertNotIn("a__compute", output)
        self.assertNotIn("b__compute", output)
        self.assertIn("c__compute", output)
        self.assertEqual(self.task_evidence("a", "b", "c", "d", "e"), before)
        preview = self.cli("run", "c", "--dry-run", success=False)
        self.assertEqual(list(test_run_selection.SelectedRunTests.actions(self, preview)), ["a", "b", "c"])

    def test_blocker_preview_hints_preserve_workflow_context_and_selected_scope(self):
        self.cli("run", "c")
        self.finish()
        damaged = next(path for path in (self.work / ".gwf/gwflow").rglob("completion.json")
                       if "a" in path.parts)
        damaged.unlink()
        damaged.mkdir()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() + "pipeline = gwf\n")
        workflow.rename(self.work / "selected workflow.py")
        environment = self.state_backend({})
        options = ("--file", "selected workflow.py:pipeline", "--backend", "state_fixture",
                   "--verbose", "warning", "--no-color")
        before = self.task_evidence("a", "b", "c", "d", "e")
        for selectors in (("c",), ("[bc]",)):
            with self.subTest(selectors=selectors):
                output = self.cli(*options, "status", *selectors, "--plain", env=environment)
                hints = re.findall(r"validation prevents proceeding; see (gwflow [^\n;]+)", output)
                self.assertTrue(hints, output)
                for hint in set(hints):
                    command = shlex.split(hint)
                    self.assertEqual(command[:len(options) + 1], ["gwflow", *options])
                    self.assertIn("--plain", command)
                    self.assertTrue(all(selector in command for selector in selectors))
                    preview = self.cli(*command[1:], env=environment, success=False)
                    self.assertEqual(list(test_run_selection.SelectedRunTests.actions(self, preview)), ["a", "b", "c"])
                    self.assertIn(str(damaged), preview)
                self.assertEqual(self.task_evidence("a", "b", "c", "d", "e"), before)


class StatusImageScopeTests(test_registry_images.RegistryTestCase):
    names = StatusScopeTests.names

    def configure_workflow(self, reference=None):
        super().configure_workflow(reference, other=True)
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

    def test_unrelated_image_failure_does_not_affect_selected_status_or_filtered_empty_result(self):
        self.options(fail=True)
        before = self.snapshot()
        output = self.cli("status", "independent", "--details")
        self.assertEqual(self.names(output), ["independent"])
        self.assertNotIn("Workflow notices", output)
        self.assertEqual(self.calls("pull"), [])
        empty = self.cli("status", "independent", "--status", "failed")
        self.assertIn("No Tasks selected", empty)
        self.assertEqual(self.calls("pull"), [])
        output = self.cli("status", "consumer")
        self.assertEqual(self.names(output), ["consumer"])
        self.assertIn("sample (outside selection)", output)
        self.assertIn("image unavailable", output)
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertEqual(self.snapshot(), before)

    def test_selected_status_acquires_upstream_images_with_cold_warm_and_reusable_cache(self):
        output = self.cli("status", "consumer", "--details")
        self.assertEqual(self.names(output), ["consumer"])
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.cli("status", "consumer")
        self.assertEqual(len(self.calls("pull")), 1)
        self.cli("run", "consumer")
        self.finish()
        self.cli("clean", "--delete")
        image, = self.cached_images()
        self.options(mtime_ns=image.stat().st_mtime_ns)
        image.unlink()
        before = self.snapshot()
        output = self.cli("status", "consumer")
        self.assertEqual(self.names(output), ["consumer"])
        self.assertRegex(output, r"(?m)^consumer\s+reusable\s+3/3")
        self.assertEqual(len(self.calls("pull")), 2)
        self.cli("status", "consumer", "--details")
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.work / "results/consumer/copy.txt").read_bytes(), b"fixture image\n")
        self.assertFalse((self.work / "results/independent").exists())
