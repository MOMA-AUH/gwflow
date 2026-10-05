"""Container input layouts and mount permissions at the public workflow boundary."""

import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from gwf.exceptions import WorkflowError
from gwflow import Task

from support import LocalBackendTestCase
import test_containers
import test_managed_recovery


def write_workflow(work, sources, *, command, outputs=("out.txt",), bound=True, stage_as=None):
    image = os.environ.get("GWFLOW_TEST_SIF", "unavailable.sif")
    bindings = ", ".join(f"source{index}={value!r}" for index, value in enumerate(sources))
    expression = f"shell({command!r}, {bindings})" if bound else repr(command)
    staging = "" if stage_as is None else f", stage_as={stage_as!r}"
    (work / "workflow.py").write_text(
        "from gwflow import Task, Workflow, shell\n"
        "gwf = Workflow()\n"
        f"task = Task(inputs={sources!r})\n"
        f"target = task.target('read', inputs={sources!r}, outputs={list(outputs)!r}, image={image!r}{staging})\n"
        f"target << {expression}\n"
        f"task.retain('result', source=target.output({outputs[0]!r}), path='result.txt')\n"
        "gwf.task_from_template('sample', task)\n"
    )


class StagingAuthoringTests(unittest.TestCase):
    def test_nonempty_staging_requires_an_image(self):
        with self.assertRaisesRegex(WorkflowError, "stage_as.*image"):
            Task(inputs=["input.txt"]).target("read", inputs=["input.txt"], outputs=["out.txt"],
                                             stage_as={"renamed.txt": "input.txt"})
        Task(inputs=["input.txt"]).target("read", inputs=["input.txt"], outputs=["out.txt"], stage_as={})

    def test_stage_as_requires_a_mapping(self):
        for value in (False, "file", 1, [("same", "input.txt"), ("same", "extra.txt")]):
            with self.subTest(value=value), self.assertRaisesRegex(WorkflowError, "stage_as.*mapping"):
                Task(inputs=["input.txt"]).target("read", inputs=["input.txt"], outputs=["out.txt"],
                                                 image="tool.sif", stage_as=value)


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

    def test_invalid_overrides_fail_before_submission(self):
        cases = (
            {"unused.txt": "undeclared.txt"},
            {"one.txt": "input.txt", "two.txt": "./input.txt"},
            {"extra.txt": "input.txt"},
            {"out.txt": "input.txt"},
            {"out.txt/nested": "input.txt"},
            {"dir": "input.txt", "dir/nested.txt": "extra.txt"},
            {"./same.txt": "input.txt", "same.txt": "extra.txt"},
            *({name: "input.txt"} for name in ("", ".", "/absolute.txt", "../escape.txt", "a/../b", "bad\nname", "bad?.txt")),
        )
        cases = [*((layout, ("out.txt",)) for layout in cases), ({"parent": "input.txt"}, ("parent/out.txt",))]
        for layout, outputs in cases:
            with self.subTest(layout=layout, outputs=outputs):
                write_workflow(self.work, ["input.txt", "extra.txt"], stage_as=layout, outputs=outputs, command="true", bound=False)
                output = self.cli("run", success=False)
                self.assertRegex(output, "stage_as|collision|Invalid managed relative path")
                self.assertFalse((self.work / ".gwf/gwflow").exists())


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerStagingTests(LocalBackendTestCase):
    snapshot = test_containers.ContainerRuntimeTests.snapshot
    attempt = test_containers.ContainerRuntimeTests.attempt
    inject = test_managed_recovery.ManagedCoordinationTests.inject

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

    def test_custom_paths_disambiguate_basenames_and_place_declared_companions_together(self):
        sources = ["sales/summary.csv", "returns/summary.csv", "genome.fa", "genome.index", "extra.txt"]
        for name, value in zip(sources[:4], ("sales\n", "returns\n", "reference\n", "index\n")):
            path = self.work / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(value)
        layout = {"sales data/summary.csv": sources[0], "returns data/summary.csv": sources[1],
                  "ref's $data/genome.fa": sources[2], "ref's $data/genome.fa.fai": sources[3]}
        checks = "".join(f"test -L {{source{index}}}; test {{source{index}}} = \"$PWD\"/{shlex.quote(name)}; "
                         for index, name in enumerate([*layout, "extra.txt"]))
        write_workflow(self.work, sources, stage_as=layout, command=(
            checks + 'test ! -e genome.fa; test ! -e summary.csv; '
            'cat {source0} {source1} {source2} {source2}.fai {source4} > out.txt'
        ))
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "sales\nreturns\nreference\nindex\nextra\n")
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_effective_layout_changes_refresh_but_equivalent_mappings_reuse(self):
        self.configure(use_spec_hashes=True)
        command = 'printf "SOURCE=%s\\n" {source0}; cat {source0} {source1} > out.txt'
        sources = ["input.txt", "extra.txt"]
        write_workflow(self.work, sources, command=command)
        self.run_complete()
        previous = self.attempt()
        for layout in ({"input.txt": "input.txt", "extra.txt": "extra.txt"},
                       {"./extra.txt": "./extra.txt", "./input.txt": "./input.txt"}):
            with self.subTest(equivalent=layout):
                write_workflow(self.work, sources, command=command, stage_as=layout)
                self.assertNotIn("Submitted target", self.cli("run"))
                self.assertEqual(self.attempt(), previous)
        self.configure(use_spec_hashes=False)
        write_workflow(self.work, sources, command=command, stage_as={"new dir/input.txt": "input.txt"})
        before = self.snapshot()
        for arguments in (("explain", "--details"), ("status", "--details"), ("run", "--dry-run", "--details")):
            self.assertIn("changed declared structure: target membership or file declarations", self.cli(*arguments))
            self.assertEqual(self.snapshot(), before)
        self.run_complete()
        self.assertNotEqual(self.attempt(), previous)
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "hello\nextra\n")
        self.assertIn("/new dir/input.txt", self.cli("logs", "sample__read", "--no-pager"))
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_layout_replacement_keeps_activity_and_ownership_guards(self):
        command = "cat {source0} > out.txt"
        write_workflow(self.work, ["input.txt"], command=command)
        self.run_complete()
        write_workflow(self.work, ["input.txt"], command=command, stage_as={"new/input.txt": "input.txt"})
        for guard in ("activity", "ownership"):
            with self.subTest(guard=guard):
                if guard == "activity":
                    backend, env = ("-b", "recovery_fixture"), self.inject(queued_prefix="sample")
                    reason = "work blocks replacement"
                else:
                    manifest = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/sample/attempts/*/**/manifest.json"))
                    manifest.write_text("{truncated")
                    backend, env, reason = (), None, "ownership"
                before = self.snapshot()
                for arguments, success in ((("explain", "--details"), True), (("status", "--details"), True),
                                           (("run", "--dry-run"), False), (("run",), False)):
                    self.assertIn(reason, self.cli(*backend, *arguments, env=env, success=success))
                    self.assertEqual(self.snapshot(), before)


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
