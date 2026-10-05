"""Managed Tasks through public authoring and installed CLI interfaces."""

from pathlib import Path
import json
import os
import shlex
import shutil
import unittest

from gwf.backends.local import Client, LocalStatus
from gwf.exceptions import WorkflowError
from gwflow import Task, Workflow

from support import LocalBackendTestCase


class ManagedAuthoringTests(unittest.TestCase):
    def test_task_rejects_retired_working_directory_option(self):
        for working_dir in (None, "."):
            with self.subTest(working_dir=working_dir), self.assertRaisesRegex(
                    TypeError, "unexpected keyword argument 'working_dir'"):
                Task(inputs=[], working_dir=working_dir)

    def test_invalid_output_paths_and_collisions_fail_during_authoring(self):
        for outputs in ([""], ["."], ["/outside"], ["../outside"], ["a/../b"], ["*.txt"],
                        [], ["a", "a"], ["a", "a/b"], ["a/b", "a//b"]):
            with self.subTest(outputs=outputs), self.assertRaises(WorkflowError):
                Task(inputs=[]).target("write", inputs=[], outputs=outputs)

    def test_retained_contract_rejects_foreign_sources_and_duplicate_names(self):
        task, other = Task(inputs=[]), Task(inputs=[])
        target = task.target("write", inputs=[], outputs=["one", "two"])
        foreign = other.target("write", inputs=[], outputs=["one"])
        with self.assertRaises(WorkflowError):
            target.output("missing")
        with self.assertRaises(WorkflowError):
            task.retain("outside", source=foreign.output("one"), path="out")
        task.retain("first", source=target.output("one"), path="out")
        for name, path in (("first", "elsewhere"), ("second", "out"), ("second", "out/nested")):
            with self.subTest(name=name, path=path), self.assertRaises(WorkflowError):
                task.retain(name, source=target.output("two"), path=path)
        with self.assertRaisesRegex(WorkflowError, "belong to a Task"):
            Workflow(working_dir=".").target("outside", inputs=[], outputs=["x"])

    def test_command_template_rejects_ambiguous_fields_and_bindings(self):
        from gwflow import shell
        from gwf.exceptions import WorkflowError
        task = Task(inputs=[])
        target = task.target("write", inputs=[], outputs=["out.txt"])
        for template in ("{}", "{0}", "{out.name}", "{out[0]}", "{out!r}", "{out:}", "{missing}"):
            with self.subTest(template=template), self.assertRaises(WorkflowError):
                shell(template, out=target.output("out.txt"))
        with self.assertRaises(WorkflowError):
            shell("echo literal", unused=target.output("out.txt"))

    def test_factory_exposes_named_retained_outputs_after_registration(self):
        task = Task(inputs=[])
        target = task.target("write", inputs=[], outputs=["nested/result.txt"])
        target << "mkdir -p nested; printf hello > nested/result.txt"
        task.retain("report", source=target.output("nested/result.txt"), path="report.txt")
        workflow = Workflow(working_dir=".")
        handle = workflow.task_from_template("sample", task)
        self.assertEqual(set(handle.outputs), {"report"})
        with self.assertRaises(KeyError):
            handle.outputs["internal"]


class ManagedCliTests(LocalBackendTestCase):
    def configure_workflow(self, **options):
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[])\n"
            "target = task.target('write', inputs=[], outputs=['nested/result.txt', 'scratch.txt'])\n"
            "target << 'mkdir -p nested; printf hello > nested/result.txt; printf private > scratch.txt'\n"
            "task.retain('report', source=target.output('nested/result.txt'), path='report.txt')\n"
            "gwf.task_from_template('sample', task)\n"
        )

    def write_task(self, command, *, outputs=("out.txt",), retained=True, settings="", name="sample"):
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow, shell\n"
            f"gwf = Workflow({settings})\n"
            "task = Task(inputs=[])\n"
            f"target = task.target('write', inputs=[], outputs={list(outputs)!r})\n"
            f"target << {command!r}\n"
            + (f"task.retain('result', source=target.output({outputs[0]!r}), path='result.txt')\n" if retained else "")
            + f"gwf.task_from_template({name!r}, task)\n"
        )

    def settle(self):
        def inactive():
            with Client.connect(port=self.port) as client:
                states = client.status()
            return states and not any(state in (LocalStatus.SUBMITTED, LocalStatus.RUNNING)
                                      for state in states.values())
        self.wait_for(inactive)

    def test_failed_or_incomplete_output_sets_never_publish_results(self):
        outside = self.work / "outside.txt"
        outside.write_text("untouched")
        cases = {
            "failed": "printf first > out.txt; printf second > other.txt; exit 9",
            "missing": "printf first > out.txt",
            "directory": "mkdir out.txt; touch other.txt",
            "symlink": f"ln -s {shlex.quote(str(outside))} out.txt; touch other.txt",
        }
        for name, command in cases.items():
            with self.subTest(name=name):
                self.write_task(command, outputs=("out.txt", "other.txt"), name=name)
                self.cli("run")
                self.settle()
                self.assertFalse((self.work / "results" / name).exists())
                self.assertIn(": retry;", self.cli("explain"))
                self.cli("run")
                self.settle()
                self.assertFalse((self.work / "results" / name).exists())
        self.assertEqual(outside.read_text(), "untouched")

    def test_template_quoting_literal_braces_and_multiple_nested_outputs(self):
        output = "nested/it's $result;!.txt"
        self.write_task("unused", outputs=(output, "fixed.txt"))
        path = self.work / "workflow.py"
        text = path.read_text().replace("target << 'unused'", "target << shell(\"mkdir -p nested; printf '{{ok}}' > {result}; printf fixed > fixed.txt\", result=target.output(" + repr(output) + "))")
        path.write_text(text)
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "{ok}")
        self.assertEqual(len(list((self.work / "work").rglob("fixed.txt"))), 1)

    def test_private_working_directory_and_managed_tmpdir_opt_out(self):
        for managed in (True, False):
            with self.subTest(managed=managed):
                name = "managed" if managed else "inherited"
                self.write_task("printf '%s\\n' \"$PWD\" \"${TMPDIR-unset}\" > out.txt",
                                settings=f"managed_tmpdir={managed}", name=name)
                self.cli("run")
                self.settle()
                cwd, temporary = (self.work / "results" / name / "result.txt").read_text().splitlines()
                self.assertTrue(Path(cwd).is_relative_to(self.work / "work"))
                if managed:
                    self.assertTrue(Path(temporary).is_relative_to(self.work / "work"))
                    self.assertTrue(Path(temporary).is_dir())
                else:
                    self.assertEqual(temporary, os.environ.get("TMPDIR", "unset"))

    def test_task_with_no_retained_files_still_checks_computation(self):
        self.write_task("printf done > out.txt", retained=False)
        self.run_complete()
        self.assertEqual(list((self.work / "results/sample").iterdir()), [])
        shutil.rmtree(self.work / "work")
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_inspection_and_generic_mutation_commands_preserve_managed_evidence(self):
        self.run_complete()
        def evidence():
            return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                    for root in (self.work / ".gwf/gwflow", self.work / "results")
                    for p in root.rglob("*") if p.is_file()}
        before = evidence()
        for command in (("explain", "--details"), ("status", "--details"), ("run", "--dry-run")):
            self.cli(*command)
            self.assertEqual(evidence(), before)
        for command in (("clean", "--all", "--force"), ("touch",)):
            self.assertIn("clean-work", self.cli(*command, success=False))
            self.assertEqual(evidence(), before)

    def test_unowned_results_and_changed_recorded_roots_are_not_adopted(self):
        result = self.work / "results/sample/report.txt"
        result.parent.mkdir(parents=True)
        result.write_text("unowned")
        self.assertIn("Unowned", self.cli("run", success=False))
        self.assertEqual(result.read_text(), "unowned")
        shutil.rmtree(result.parent)
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("Workflow()", "Workflow(work_root='elsewhere')"))
        self.assertIn("recorded storage locations", self.cli("run", success=False))
        self.assertFalse((self.work / "elsewhere").exists())
        self.assertEqual(result.read_text(), "hello")

    def test_malformed_transfer_evidence_cannot_establish_completion(self):
        self.run_complete()
        manifest = next(p for p in (self.work / ".gwf/gwflow").rglob("*.json")
                        if json.loads(p.read_text()).get("kind") == "gwflow.manifest")
        original = manifest.read_bytes()
        damaged = json.loads(original)
        del damaged["sources"]
        manifest.write_text(json.dumps(damaged))
        self.assertIn("blocked", self.cli("explain"))
        self.cli("run", success=False)
        manifest.write_bytes(original)
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_runtime_work_root_substitution_cannot_redirect_managed_commit(self):
        started, release = self.work / "started", self.work / "release"
        self.write_task(f"touch {shlex.quote(str(started))}; "
                        f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.02; done; "
                        "printf done > out.txt")
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("untouched")
        self.cli("run")
        self.wait_for(started.exists)
        (self.work / "work").rename(self.work / "saved-work")
        (self.work / "work").symlink_to(outside, target_is_directory=True)
        release.touch()
        self.settle()
        self.assertEqual([p.name for p in outside.iterdir()], ["sentinel"])
        self.assertEqual((outside / "sentinel").read_text(), "untouched")
        self.assertFalse((self.work / "results/sample").exists())

    def test_runtime_results_substitution_is_left_untouched(self):
        started, release = self.work / "started", self.work / "release"
        self.write_task(f"touch {shlex.quote(str(started))}; "
                        f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.02; done; "
                        "printf done > out.txt")
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("untouched")
        self.cli("run")
        self.wait_for(started.exists)
        destination = self.work / "results/sample"
        destination.symlink_to(outside, target_is_directory=True)
        release.touch()
        self.settle()
        self.assertTrue(destination.is_symlink())
        self.assertEqual([p.name for p in outside.iterdir()], ["sentinel"])
        self.assertIn("blocked", self.cli("explain"))

    def test_nested_output_symlinks_cannot_supply_checked_outputs(self):
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "out.txt").write_text("not-produced")
        self.write_task(f"ln -s {shlex.quote(str(outside))} nested", outputs=("nested/out.txt",))
        self.cli("run")
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        self.assertEqual((outside / "out.txt").read_text(), "not-produced")

    def test_unknown_bindings_and_undeclared_inputs_fail_before_initialization(self):
        self.write_task("unused")
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        workflow.write_text(original.replace("target << 'unused'", "other = Task(inputs=[]).target('other', inputs=[], outputs=['out.txt'])\ntarget << shell('cat {outside}', outside=other.output('out.txt'))"))
        self.assertIn("not a declared input or output", self.cli("run", success=False))
        self.assertFalse((self.work / "work").exists())
        workflow.write_text(original.replace("target('write', inputs=[]", "target('write', inputs=['input.txt']"))
        self.assertIn("undeclared Task boundary input", self.cli("run", success=False))
        self.assertFalse((self.work / "work").exists())

    def test_legacy_and_truncated_records_cannot_supply_ownership(self):
        book = self.work / ".gwf/gwflow"
        book.mkdir(parents=True)
        record = book / "owner.json"
        for content in ('{"schema":1,"attempt":"' + "a" * 32 + '","task":"sample","definition":{}}', '{"kind":'):
            with self.subTest(content=content):
                record.write_text(content)
                self.cli("run", success=False)
                self.assertFalse((self.work / "work").exists())
                self.assertEqual(record.read_text(), content)

    def test_command_tracking_and_definition_changes_do_not_adopt_old_results(self):
        self.write_task("printf first > out.txt")
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf first", "printf second"))
        self.assertNotIn("Submitted target", self.cli("run"))
        self.configure(use_spec_hashes=True)
        self.assertIn("commands", self.cli("explain"))
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "second")

    def test_declared_storage_overlaps_fail_before_initialization(self):
        for settings in ("work_root='results/work'", "results_root='.gwf/results'", "work_root='same', results_root='same'"):
            with self.subTest(settings=settings):
                self.write_task("touch out.txt", settings=settings)
                self.assertIn("disjoint", self.cli("run", success=False))
                self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_equivalent_root_spellings_and_resource_changes_preserve_reuse(self):
        self.write_task("echo log-message; touch out.txt")
        self.run_complete()
        self.write_task("echo log-message; touch out.txt", settings="work_root='./work/', results_root='results/.', defaults={'cores': 3}")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertIn("log-message", self.cli("logs", "sample__write", "--no-pager"))

    def test_registered_factories_are_independent_snapshots(self):
        self.write_task("printf first > out.txt")
        with (self.work / "workflow.py").open("a") as stream:
            stream.write("gwf.task_from_template('second', task)\n"
                         "target.spec = 'false'\n"
                         "target.outputs.append('missing.txt')\n")
        self.run_complete()
        for name in ("sample", "second"):
            self.assertEqual((self.work / "results" / name / "result.txt").read_text(), "first")

    def test_declaration_traversal_before_validation_cannot_publish_attempts(self):
        with (self.work / "workflow.py").open("a") as stream:
            stream.write("list(gwf.targets.values())\nraise RuntimeError('invalid declaration')\n")
        self.assertIn("invalid declaration", self.cli("run", success=False))
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.assertFalse((self.work / "work").exists())

    def test_failed_target_without_retained_files_cannot_complete(self):
        self.write_task("exit 1", retained=False)
        self.cli("run")
        self.settle()
        self.assertIn(": retry;", self.cli("explain"))
        self.assertFalse((self.work / "results/sample").exists())

    def test_retained_results_survive_work_removal_and_are_reused(self):
        initial = self.cli("explain")
        self.assertIn("fresh", initial)
        self.assertIn("Would submit sample__write", self.cli("run", "--dry-run"))
        self.assertFalse((self.work / "work").exists())
        self.run_complete()
        result = self.work / "results" / "sample" / "report.txt"
        self.assertEqual(result.read_text(), "hello")
        self.assertEqual([p.relative_to(self.work / "results").as_posix()
                          for p in (self.work / "results").rglob("*") if p.is_file()],
                         ["sample/report.txt"])
        copies = list((self.work / "work").rglob("result.txt"))
        self.assertEqual(len(copies), 1)
        self.assertNotEqual(copies[0].stat().st_ino, result.stat().st_ino)
        shutil.rmtree(self.work / "work")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(result.read_text(), "hello")
        self.assertFalse((self.work / "work").exists())
        self.assertRegex(self.cli("status"), r"Task sample\s+reusable\s+3/3\s+work cleaned")
        self.assertIn("reuse", self.cli("explain"))
