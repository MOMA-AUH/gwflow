"""Logical input declarations and frozen baselines through the installed local CLI."""

import hashlib
import json
import os
from pathlib import Path
import textwrap
import unittest
import zipfile

from gwf.exceptions import WorkflowError
from gwflow import Task

from record_fixtures import read_record, record_path, remove_record, write_record
from support import LocalBackendTestCase
import test_inputs
import test_inspection


class LogicalInputTests(LocalBackendTestCase):
    inject = test_inputs.ExternalInputTests.inject
    fault = test_inputs.ExternalInputTests.fault
    snapshot = test_inspection.LifecycleInspectionTests.snapshot

    def configure_workflow(self, *, identity='"sample-1"', metadata='{"encoding": "utf-8"}',
                           files='{"left": "input.txt", "right": "extra.txt"}'):
        (self.work / "workflow.py").write_text(textwrap.dedent(f"""\
            from gwflow import Task, Workflow, shell, task_template

            @task_template
            def sample():
                task = Task(inputs=["decode.py"])
                pair = task.logical_input("pair", identity={identity}, files={files}, metadata={metadata})
                decode = task.target("decode", inputs=[pair, "decode.py"], outputs=["decoded"])
                decode << shell("python {{script}} {{descriptor}} > decoded",
                                script="decode.py", descriptor=pair)
                keep = task.target("keep", inputs=[decode.output("decoded")], outputs=["result"])
                keep << shell("cat {{decoded}} > result", decoded=decode.output("decoded"))
                task.retain("result", source=keep.output("result"), path="result")
                return task

            gwf = Workflow()
            registered = gwf.task(sample())
        """))
        script = self.work / "decode.py"
        if not script.exists():
            script.write_text(textwrap.dedent("""\
                from pathlib import Path
                import json
                import sys
                import zipfile

                descriptor = json.loads(Path(sys.argv[1]).read_text())
                files = descriptor["files"]
                print(descriptor["identity"])
                encoding = descriptor["metadata"]["encoding"]
                if "archive" in files:
                    with zipfile.ZipFile(files["archive"]) as archive:
                        parts = [archive.read(key).decode(encoding) for key in ("left", "right")]
                else:
                    parts = [Path(files[key]).read_text(encoding=encoding) for key in ("left", "right")]
                print(":".join(part.strip() for part in parts))
                print(sys.argv[1], file=sys.stderr)
            """))

    def test_descriptor_consumes_named_files_and_reuse_survives_cleanup(self):
        self.run_complete()
        result = self.work / "results/sample/result"
        self.assertEqual(result.read_text(), "sample-1\nhello:extra\n")
        attempt = self.task_attempts()["sample"]
        log = self.cli("logs", "sample__decode", "--stderr", "--no-pager")
        descriptor = Path(next(line for line in log.splitlines() if line.endswith(".json")))
        self.assertTrue(descriptor.is_relative_to(self.work / "work"))
        frozen_descriptor = descriptor.read_bytes(), descriptor.stat().st_mtime_ns
        self.configure_workflow(metadata='{"encoding": "ascii"}')
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual((descriptor.read_bytes(), descriptor.stat().st_mtime_ns), frozen_descriptor)
        self.cli("clean", "sample", "--delete")
        self.assertFalse(descriptor.exists())
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual(self.task_attempts()["sample"], attempt)
        self.assertEqual(result.read_text(), "sample-1\nhello:extra\n")

    def test_switch_to_archive_reuses_cleaned_attempt_and_force_consumes_archive(self):
        self.run_complete()
        original = self.task_attempts()["sample"]
        log = ("logs", "sample__decode", "--stderr", "--no-pager")
        descriptor = Path(next(line for line in self.cli(*log).splitlines() if line.endswith(".json")))
        self.cli("clean", "sample", "--delete")
        self.assertFalse(descriptor.exists())
        archive = self.work / "pair.zip"
        with zipfile.ZipFile(archive, "w") as packed:
            for role, filename in (("left", "input.txt"), ("right", "extra.txt")):
                packed.write(self.work / filename, role)
        self.configure_workflow(files='{"archive": "pair.zip"}', metadata='{"encoding": "ascii"}')
        (self.work / "input.txt").unlink()
        (self.work / "extra.txt").unlink()

        frozen_records = self.snapshot()
        status = json.loads(self.cli_result("status", "--json").stdout)
        self.assertEqual(status["tasks"][0]["next_action"], "reuse")
        self.assertIn("Reuse", self.cli("run", "--dry-run"))
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual(self.task_attempts()["sample"], original)
        self.assertEqual(self.snapshot(), frozen_records)
        self.assertFalse(descriptor.exists())
        result = self.work / "results/sample/result"
        self.assertEqual(result.read_text(), "sample-1\nhello:extra\n")

        self.run_complete("sample", "--force")
        self.assertNotEqual(self.task_attempts()["sample"], original)
        replacement = Path(next(line for line in self.cli(*log).splitlines() if line.endswith(".json")))
        self.assertEqual(json.loads(replacement.read_text())["files"], {"archive": str(archive)})
        self.assertEqual(result.read_text(), "sample-1\nhello:extra\n")

    def test_changed_logical_identity_starts_a_fresh_attempt(self):
        self.configure_workflow(identity='{"sample": 1, "verified": True}')
        self.run_complete()
        original = self.task_attempts()["sample"]
        self.configure_workflow(identity='{"verified": True, "sample": 1}',
                                files='{"right": "extra.txt", "left": "input.txt"}')
        self.assertNoSubmissions(self.cli("run"))

        self.configure_workflow(identity='{"sample": 1, "verified": 1}')
        self.assertIn("changed logical input name or identity", self.cli("run", "--dry-run"))
        self.run_complete()
        changed_identity = self.task_attempts()["sample"]
        self.assertNotEqual(original, changed_identity)

    def test_only_current_representation_is_required_and_ordinary_inputs_stay_tracked(self):
        workflow = self.work / "workflow.py"
        # The left file also has an independent role as an ordinary Task input.
        workflow.write_text(workflow.read_text().replace('Task(inputs=["decode.py"])',
                                                          'Task(inputs=["decode.py", "input.txt"])'))
        self.run_complete()
        self.cli("clean", "sample", "--delete")
        original = self.task_attempts()["sample"]
        (self.work / "extra.txt").write_text("new physical contents\n")
        self.assertNoSubmissions(self.cli("run"))
        workflow.write_text(workflow.read_text().replace('"right": "extra.txt"', '"right": "alternate.txt"'))
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual(status["reason"]["code"], "input_unavailable")
        self.assertIn("alternate.txt", status["reason"]["message"])
        (self.work / "extra.txt").rename(self.work / "alternate.txt")
        self.assertNoSubmissions(self.cli("run"))
        (self.work / "input.txt").write_text("ordinary change\n")
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual((status["attempt_id"], status["next_action"], status["reason"]["code"]),
                         (original, "run", "inputs_changed"))

    def test_consumer_uses_retained_output_after_producer_switches_representation(self):
        self.run_complete()
        producer = self.task_attempts()["sample"]
        self.cli("clean", "sample", "--delete")
        (self.work / "input.txt").rename(self.work / "selected.txt")
        self.configure_workflow(files='{"left": "selected.txt", "right": "extra.txt"}')
        with (self.work / "workflow.py").open("a") as workflow:
            workflow.write(textwrap.dedent("""
                @task_template
                def copy(source):
                    task = Task(inputs=[source])
                    write = task.target("write", inputs=[source], outputs=["copied"])
                    write << shell("cat {source} > copied", source=source)
                    task.retain("copy", source=write.output("copied"), path="copied")
                    return task

                gwf.task(copy(registered.outputs["result"]))
            """))
        self.run_complete("copy")
        self.assertEqual((self.work / "results/copy/copied").read_text(), "sample-1\nhello:extra\n")
        attempts = self.task_attempts()
        self.assertEqual(attempts["sample"], producer)
        self.assertNoSubmissions(self.cli("run", "copy"))
        self.run_complete("sample", "--force")
        self.assertIn("producer sample attempt changed", self.cli("run", "copy", "--dry-run"))
        self.run_complete("copy")
        self.assertNotEqual(self.task_attempts()["copy"], attempts["copy"])

    def test_changed_file_during_decoding_cannot_be_accepted_on_retry(self):
        # Hold the workflow-owned decoder after it receives its descriptor.
        script = self.work / "decode.py"
        script.write_text(script.read_text().replace("files = descriptor", textwrap.dedent("""\
            import time
            root = Path(__file__).parent
            (root / "decoding").touch()
            while not (root / "resume").exists():
                time.sleep(0.05)
            files = descriptor""")))
        source = self.work / "extra.txt"
        original, stat = source.read_bytes(), source.stat()
        self.cli("run")
        self.wait_for(lambda: (self.work / "decoding").exists())
        try:
            source.write_text("different bytes")
        finally:
            (self.work / "resume").touch()
        self.settle()
        self.assertIn("changed after preparation",
                      self.cli("logs", "sample__decode", "--stderr", "--no-pager"))
        self.assertFalse((self.work / "results/sample").exists())
        source.write_bytes(original)
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result").read_text(), "sample-1\nhello:extra\n")

    def test_malformed_persisted_file_selection_cannot_select_work(self):
        self.run_complete()
        path = record_path(self.work, "sample", "attempt.json")
        attempt = read_record(path)
        # A self-consistent fingerprint alone cannot establish a usable declaration.
        attempt["structure"]["logical_inputs"]["pair"]["representation"]["files"] = {"left": "relative"}
        definition = {"structure": attempt["structure"], "commands": attempt["commands"]}
        attempt["fingerprint"] = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        write_record(path, attempt)
        output = self.cli("run", success=False)
        self.assertIn("attempt", output)
        self.assertNotIn("failed unexpectedly", output)
        self.assertNoSubmissions(output)
        self.assertEqual((self.work / "results/sample/result").read_text(), "sample-1\nhello:extra\n")

    def test_equivalent_representation_still_requires_valid_cleanup_evidence(self):
        self.run_complete()
        self.cli("clean", "sample", "--delete")
        path = record_path(self.work, "sample", "cleanup.json")
        record = read_record(path)
        write_record(path, {**record, "removed_paths": ["outside the recorded directories"]})
        self.configure_workflow(metadata='{"encoding": "ascii"}')
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual(status["next_action"], "blocked")
        self.assertIn("cleanup progress", status["reason"]["message"])

    def test_cleaned_attempt_without_input_baseline_can_start_fresh(self):
        self.run_complete()
        original = self.task_attempts()["sample"]
        self.cli("clean", "sample", "--delete")
        remove_record(record_path(self.work, "sample", "inputs.json"))
        self.assertIn("fresh attempt", self.cli("run", "--dry-run"))
        self.run_complete()
        self.assertNotEqual(self.task_attempts()["sample"], original)

    def test_active_attempt_freezes_selection_and_blocks_replacement(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_baseline=True))
        self.wait_for(lambda: (self.work / "baseline-held").exists())
        try:
            self.configure_workflow(files='{"left": "extra.txt", "right": "input.txt"}')
            for args in (("status", "--json"), ("run", "--dry-run", "--details"), ("run",)):
                output = self.cli(*args, success=args[0] == "status")
                self.assertIn("blocks replacement", output)
                self.assertIn("previous physical baseline", output)
        finally:
            (self.work / "baseline-release").touch()
        self.finish()
        result = self.work / "results/sample/result"
        self.assertEqual(result.read_text(), "sample-1\nhello:extra\n")
        original = self.task_attempts()["sample"]
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual(original, self.task_attempts()["sample"])

    def test_failed_attempt_cannot_retry_with_a_different_representation(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault("keep", crash_before_commit=True))
        self.settle()
        original = self.task_attempts()["sample"]
        self.configure_workflow(files='{"left": "extra.txt", "right": "input.txt"}')
        preview = self.cli("run", "--dry-run", "--details")
        self.assertIn("previous physical baseline", preview)
        self.assertIn("fresh attempt", preview)
        self.cli("run")
        self.settle()
        self.assertNotEqual(self.task_attempts()["sample"], original)
        self.assertEqual((self.work / "results/sample/result").read_text(), "sample-1\nextra:hello\n")

    def test_unchanged_retry_preserves_checked_decoder_execution(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault("keep", crash_before_commit=True))
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        original = self.task_attempts()["sample"]
        log = ("logs", "sample__decode", "--stderr", "--no-pager")
        decoder = self.cli(*log)
        self.assertIn("/descriptors/", decoder)
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result").read_text(), "sample-1\nhello:extra\n")
        self.assertEqual(self.task_attempts()["sample"], original)
        self.assertEqual(self.cli(*log), decoder)

    def test_redirected_member_with_matching_size_and_mtime_blocks_computation(self):
        alias = self.work / "alias"
        alias.symlink_to("input.txt")
        source = (self.work / "input.txt").stat()
        other = self.work / "other.txt"
        other.write_text("other\n")
        os.utime(other, ns=(source.st_atime_ns, source.st_mtime_ns))
        self.configure_workflow(files='{"left": "alias", "right": "extra.txt"}')
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_baseline=True))
        self.wait_for(lambda: (self.work / "baseline-held").exists())
        try:
            alias.unlink()
            alias.symlink_to(other)
        finally:
            (self.work / "baseline-release").touch()
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        self.assertIn("changed after preparation", self.cli("logs", "sample__decode", "--stderr", "--no-pager"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result").read_text(), "sample-1\nother:extra\n")

    def test_missing_member_before_installation_blocks_completion(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault("gwflow_complete", gate_after_manifest=True))
        self.wait_for(lambda: (self.work / "manifest-held").exists())
        try:
            (self.work / "extra.txt").unlink()
        finally:
            (self.work / "manifest-release").touch()
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        self.assertIn("extra.txt", self.cli("logs", "sample__gwflow_complete", "--stderr", "--no-pager"))
        self.assertIn("extra.txt", self.cli("run", success=False))

    def test_descriptor_binding_requires_a_target_input_from_its_own_task(self):
        workflow = self.work / "workflow.py"
        declaration = workflow.read_text()
        for source, replacement, error in [
            ('inputs=[pair, "decode.py"]', 'inputs=["decode.py"]', "not a declared target input"),
            ('inputs=[pair, "decode.py"]', 'inputs=["input.txt", "decode.py"]', "undeclared Task boundary"),
            ('inputs=[pair, "decode.py"]', 'inputs=[Task([]).logical_input("pair", identity="sample-1", '
             'files={"left": "input.txt"}, metadata={}), "decode.py"]', "must belong to"),
            ('outputs=["decoded"]', 'outputs=["decoded"], image="local.sif"', "without image="),
        ]:
            with self.subTest(error=error):
                workflow.write_text(declaration.replace(source, replacement))
                self.assertIn(error, self.cli("run", success=False))
                self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_logical_input_member_must_be_outside_managed_storage(self):
        destination = self.work / "results/nested/file"
        destination.parent.mkdir(parents=True)
        destination.write_text("untouched")
        (self.work / "external").symlink_to(destination)
        self.configure_workflow(files='{"left": "external", "right": "input.txt"}')
        self.assertIn("managed storage", self.cli("run", success=False))
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.assertEqual(destination.read_text(), "untouched")


class LogicalDeclarationTests(unittest.TestCase):
    def test_rejects_invalid_values_when_the_input_is_declared(self):
        recursive = []
        recursive.append(recursive)
        for field, invalid in {
            "identity": [object(), {"nested": {1: "ambiguous key"}}, recursive],
            "files": [None, [], {}, {"": "file"}, {1: "file"}, {"part": ""},
                      {"part": object()}, {"part": b"opaque"}, {"part": "bad\x00path"}],
            "metadata": [float("nan"), [float("inf")], {"unordered"}, ("tuple",)],
        }.items():
            for value in invalid:
                with self.subTest(field=field, value=value):
                    arguments = {"identity": "example", "files": {"one": "one.txt"}, "metadata": {}}
                    arguments[field] = value
                    with self.assertRaisesRegex(WorkflowError, field):
                        Task([]).logical_input("example", **arguments)

    def test_logical_names_are_stable_and_unique_within_a_task(self):
        task = Task([])
        args = dict(files={"left": Path("reads")}, identity={"sample": 1}, metadata=None)
        for name in (None, "", "space here", "../escape"):
            with self.subTest(name=name), self.assertRaisesRegex(WorkflowError, "logical input name"):
                task.logical_input(name, **args)
        task.logical_input("reads", **args)
        with self.assertRaisesRegex(WorkflowError, "Duplicate logical input name 'reads'"):
            task.logical_input("reads", **args)
