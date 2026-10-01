"""Container input layouts and mount permissions at the public workflow boundary."""

import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from support import LocalBackendTestCase


def write_workflow(work, sources, *, command, outputs=("out.txt",), bound=True):
    image = os.environ.get("GWFLOW_TEST_SIF", "unavailable.sif")
    bindings = ", ".join(f"source{index}={value!r}" for index, value in enumerate(sources))
    expression = f"shell({command!r}, {bindings})" if bound else repr(command)
    (work / "workflow.py").write_text(
        "from gwflow import Task, Workflow, shell\n"
        "gwf = Workflow()\n"
        f"task = Task(inputs={sources!r})\n"
        f"target = task.target('read', inputs={sources!r}, outputs={list(outputs)!r}, image={image!r})\n"
        f"target << {expression}\n"
        f"task.retain('result', source=target.output({outputs[0]!r}), path='result.txt')\n"
        "gwf.task_from_template('sample', task)\n"
    )


class DefaultStagingAuthoringTests(LocalBackendTestCase):
    def configure_workflow(self):
        write_workflow(self.work, ["input.txt"], command="cat {source0} > out.txt")

    def test_ambiguous_or_invalid_default_layouts_fail_before_submission(self):
        cases = (
            (["first/input.txt", "second/input.txt"], ["out.txt"]),
            (["input.txt"], ["input.txt"]),
            (["input.txt"], ["input.txt/nested.txt"]),
            (["invalid?.txt"], ["out.txt"]),
            (["bad\nname"], ["out.txt"]),
        )
        for sources, outputs in cases:
            with self.subTest(sources=sources, outputs=outputs):
                write_workflow(self.work, sources, outputs=outputs, command="true", bound=False)
                output = self.cli("run", success=False)
                self.assertRegex(output, "collision|Invalid managed relative path")
                self.assertFalse((self.work / "work").exists())
                self.assertFalse((self.work / "results").exists())


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerStagingTests(LocalBackendTestCase):
    def configure_workflow(self):
        write_workflow(self.work, ["input.txt"], command=(
            'test -L input.txt; test -L {source0}; '
            'if echo corrupted > input.txt; then exit 1; fi; '
            'echo scratch > "$TMPDIR/scratch"; cp {source0} copy.txt; '
            'echo editable >> copy.txt; cat copy.txt > out.txt'
        ))

    def test_source_parent_with_private_work_is_readonly_but_work_and_scratch_are_writable(self):
        self.run_complete()
        self.assertEqual((self.work / "input.txt").read_text(), "hello\n")
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "hello\neditable\n")
        self.assertEqual(len(list((self.work / "work").rglob("scratch"))), 1)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_alias_through_linked_parent_stages_basename_with_shell_punctuation(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow source ',\"$: ")
        self.addCleanup(temporary.cleanup)
        outside = Path(temporary.name)
        actual = outside / "actual data.txt"
        actual.write_text("external destination\n")
        (outside / "alias's $name.txt").symlink_to(actual)
        parent = self.work / "linked parent"
        parent.symlink_to(outside, target_is_directory=True)
        source = str(parent / "alias's $name.txt")
        write_workflow(self.work, [source], command=(
            'test -L {source0}; test "$(dirname {source0})" = "$PWD"; '
            f'test "$(basename {{source0}})" = {shlex.quote(Path(source).name)}; '
            'if echo corrupted > {source0}; then exit 1; fi; cat {source0} > out.txt'
        ))
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "external destination\n")
        self.assertEqual(actual.read_text(), "external destination\n")
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_only_declared_inputs_are_staged_and_neighbors_are_not_dependencies(self):
        (self.work / "input.txt.fai").write_text("undeclared companion\n")
        (self.work / "companion.txt").write_text("declared companion\n")
        command = (
            'test -L input.txt; test -L companion.txt; test ! -e input.txt.fai; '
            f'cat input.txt companion.txt {shlex.quote(str(self.work / "extra.txt"))} > out.txt'
        )
        write_workflow(self.work, ["input.txt", "companion.txt"], command=command, bound=False)
        self.run_complete()
        result = self.work / "results/sample/result.txt"
        self.assertEqual(result.read_text(), "hello\ndeclared companion\nextra\n")
        (self.work / "extra.txt").write_text("neighbor changed\n")
        write_workflow(self.work, ["companion.txt", "input.txt", "input.txt"], command=command, bound=False)
        self.assertNotIn("Submitted target", self.cli("run"))
        (self.work / "companion.txt").write_text("companion changed\n")
        self.run_complete()
        self.assertEqual(result.read_text(), "hello\ncompanion changed\nneighbor changed\n")
        self.cli("clean-work", "--delete")
        self.assertNotIn("Submitted target", self.cli("run"))


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerSiteMountTests(LocalBackendTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow site mount ")
        self.addCleanup(temporary.cleanup)
        self.site = Path(temporary.name)
        (self.site / "site.txt").write_text("site data\n")
        with patch.dict(os.environ, {"APPTAINER_BINDPATH": f"{self.site}:/gwflow-site:ro"}):
            super().setUp()

    def configure_workflow(self):
        write_workflow(self.work, ["input.txt"], command="cat {source0} /gwflow-site/site.txt > out.txt")

    def test_deployment_mount_remains_available_beside_required_input_mounts(self):
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "hello\nsite data\n")
