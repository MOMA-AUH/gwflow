"""Logical input declarations and frozen baselines through the installed local CLI."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
import zipfile

from gwf.exceptions import WorkflowError
from gwflow import Task

from record_fixtures import read_record, record_path, remove_record, write_record
from support import FIXTURES, LocalBackendTestCase
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

    def test_selected_replacement_rechecks_sources_before_removing_results(self):
        self.run_complete()
        source = self.work / "extra.txt"
        result = self.work / "results/sample/result"
        command = [sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work),
                   "gate_before_removal", "sample", "run", "sample", "--force"]
        with subprocess.Popen(command, cwd=self.work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as run:
            try:
                self.wait_for(lambda: (self.work / "frontend-held").exists())
                source.unlink()
            finally:
                (self.work / "frontend-release").touch()
            output, _ = run.communicate(timeout=30)
        self.assertNotEqual(run.returncode, 0, output)
        self.assertIn("extra.txt", output)
        self.assertNoSubmissions(output)
        self.assertEqual(result.read_text(), "sample-1\nhello:extra\n")
        evidence = self.snapshot()
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual((status["next_action"], status["reason"]["code"]), ("blocked", "input_unavailable"))
        self.assertIn("extra.txt", self.cli("run", "--dry-run", success=False))
        self.assertIn("extra.txt", self.cli("run", success=False))
        self.assertEqual(self.snapshot(), evidence)
        source.write_text("restored\n")
        self.run_complete()
        self.assertEqual(result.read_text(), "sample-1\nhello:restored\n")

    def test_cleaned_attempt_reuses_logical_identity_without_physical_sources(self):
        self.run_complete()
        self.cli("clean", "sample", "--delete")
        (self.work / "extra.txt").rename(self.work / "replacement.txt")
        files = '{"left": "input.txt", "right": "replacement.txt"}'
        self.configure_workflow(files=files)
        evidence = self.snapshot()
        self.assertIn("sample: reusable", self.cli("wait", "sample"))
        (self.work / "replacement.txt").rename(self.work / "absent.txt")
        (self.work / "input.txt").unlink()
        self.assertIn("sample: reusable", self.cli("wait", "sample"))
        self.assertEqual(json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]["next_action"], "reuse")
        self.assertIn("Reuse", self.cli("run", "--dry-run"))
        self.assertNoSubmissions(self.cli("run"))
        self.assertIn("input.txt", self.cli("run", "--force", success=False))
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("cat {decoded}", "cat -n {decoded}"))
        self.assertIn("input.txt", self.cli("run", "--dry-run", success=False))
        self.assertIn("input.txt", self.cli("run", success=False))
        self.configure_workflow(files=files, identity='"another sample"')
        self.assertIn("invalidated: changed logical input name or identity", self.cli("wait", "sample", success=False))
        self.assertIn("input.txt", self.cli("run", success=False))
        self.assertEqual(self.snapshot(), evidence)

    def test_wait_keeps_polling_for_unconfirmed_completion_after_an_equivalent_switch(self):
        self.run_complete()
        self.configure_workflow(metadata='{"encoding": "ascii"}')
        self.stop_worker(self.worker)
        (self.work / ".gwf/local-termination/records.json").unlink()
        result = self.cli_result("wait", "sample", "--timeout", "5", success=False)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("Scheduler unavailable", result.stderr)
        self.configure_workflow(identity='"another sample"', metadata='{"encoding": "ascii"}')
        result = self.cli_result("wait", "sample", "--timeout", "5", success=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("invalidated: changed logical input name or identity", result.stderr)

    def test_wait_reports_an_unavailable_image_after_an_equivalent_switch(self):
        self.run_complete()
        self.configure_workflow(metadata='{"encoding": "ascii"}')
        (self.work / "extra.txt").unlink()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace('outputs=["result"])', 'outputs=["result"], image="missing.sif")'))
        result = self.cli_result("wait", "sample", success=False)
        self.assertIn("image unavailable", result.stderr)
        self.assertIn("missing.sif", result.stderr)

    def test_an_ordinary_input_also_used_as_a_logical_source_stays_tracked(self):
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
        self.assertNoSubmissions(self.cli("run"))
        (self.work / "extra.txt").rename(self.work / "alternate.txt")
        self.assertNoSubmissions(self.cli("run"))
        (self.work / "input.txt").write_text("ordinary change\n")
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"][0]
        self.assertEqual((status["attempt_id"], status["next_action"], status["reason"]["code"]),
                         (original, "run", "inputs_changed"))
        (self.work / "input.txt").unlink()
        self.assertIn("input.txt", self.cli("run", success=False))

    def add_consumer(self):
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

    def test_consumer_uses_retained_output_after_producer_switches_representation(self):
        self.run_complete()
        producer = self.task_attempts()["sample"]
        self.cli("clean", "sample", "--delete")
        (self.work / "input.txt").rename(self.work / "selected.txt")
        self.configure_workflow(files='{"left": "selected.txt", "right": "extra.txt"}')
        self.add_consumer()
        self.run_complete("copy")
        self.assertEqual((self.work / "results/copy/copied").read_text(), "sample-1\nhello:extra\n")
        attempts = self.task_attempts()
        self.assertEqual(attempts["sample"], producer)
        self.assertNoSubmissions(self.cli("run", "copy"))
        self.run_complete("sample", "--force")
        self.assertIn("producer sample attempt changed", self.cli("run", "copy", "--dry-run"))
        self.run_complete("copy")
        self.assertNotEqual(self.task_attempts()["copy"], attempts["copy"])

    def test_repair_without_logical_sources_defers_and_preserves_consumer(self):
        self.add_consumer()
        self.run_complete()
        attempts = self.task_attempts()
        retained = self.work / "results/sample/result"
        retained.unlink()
        (self.work / "extra.txt").unlink()
        records = self.snapshot()
        status = json.loads(self.cli_result("status", "--json").stdout)["tasks"]
        self.assertEqual({task["name"]: task["next_action"] for task in status}, {"sample": "repair", "copy": "defer"})
        self.assertIn("Repair", self.cli("run", "--dry-run"))
        self.assertEqual(self.snapshot(), records)
        self.cli("run")
        self.settle()
        self.assertEqual(retained.read_text(), "sample-1\nhello:extra\n")
        self.assertIn("copy: reusable", self.cli("wait", "copy"))
        self.assertNoSubmissions(self.cli("run", "copy"))
        self.assertEqual(self.task_attempts(), attempts)

    def test_active_repair_waits_with_only_its_retained_work_source(self):
        self.add_consumer()
        self.run_complete()
        (self.work / "extra.txt").unlink()
        intermediate, = (self.work / "work/sample").glob("*/decode/*/committed/decoded")
        intermediate.unlink()
        retained = self.work / "results/sample/result"
        retained.unlink()
        self.cli("-b", "recovery_fixture", "run", "sample",
                 env=self.fault("gwflow_complete", gate_after_manifest=True, gate_before_compaction=True))
        self.wait_for(lambda: (self.work / "manifest-held").exists())
        try:
            status = json.loads(self.cli_result("status", "sample", "--json").stdout)["tasks"][0]
            self.assertEqual(status["next_action"], "wait", status)
            self.assertEqual(self.cli_result("wait", "sample", "--timeout", "2", success=False).returncode, 124)
        finally:
            (self.work / "manifest-release").touch()
        self.wait_for((self.work / "sample-compaction-held").exists)
        try:
            status = json.loads(self.cli_result("status", "--json").stdout)["tasks"]
            self.assertEqual({t["name"]: t["next_action"] for t in status}, {"sample": "wait", "copy": "defer"})
        finally:
            (self.work / "sample-compaction-release").touch()
        self.settle()
        self.assertEqual(retained.read_text(), "sample-1\nhello:extra\n")

    def test_failed_repair_requires_available_sources_for_fresh_producer_and_consumer(self):
        self.add_consumer()
        self.run_complete()
        attempts = self.task_attempts()
        retained = self.work / "results/sample/result"
        retained.write_text("damaged output\n")
        source = self.work / "extra.txt"
        source.unlink()
        self.cli("-b", "recovery_fixture", "run", "sample",
                 env=self.fault("gwflow_complete", crash_before_manifest=True))
        self.settle()
        self.assertIn("restart retained-result copying", self.cli("run", "--dry-run"))
        checked, = (self.work / "work/sample").glob("*/keep/*/committed/result")
        checked.unlink()
        records = self.snapshot()
        blocked = self.cli("run", success=False)
        self.assertIn("extra.txt", blocked)
        self.assertNoSubmissions(blocked)
        self.assertEqual(self.snapshot(), records)
        source.write_text("replacement\n")
        self.cli("run", "sample")
        self.settle()
        self.assertNotEqual(self.task_attempts()["sample"], attempts["sample"])
        self.assertIn("producer sample attempt changed", self.cli("run", "copy", "--dry-run"))
        self.cli("run", "copy")
        self.settle()
        self.assertNotEqual(self.task_attempts()["copy"], attempts["copy"])
        self.assertEqual((self.work / "results/copy/copied").read_text(), "sample-1\nhello:replacement\n")

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
            wait = self.cli("wait", "sample", "--timeout", "5", success=False)
            self.assertIn("invalidated: changed logical input representation", wait)
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
        (self.work / "extra.txt").rename(self.work / "away")
        self.assertIn("extra.txt", self.cli("run", success=False))
        (self.work / "away").rename(self.work / "extra.txt")
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

    def test_checked_installed_set_can_finish_without_logical_sources(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault("gwflow_complete", crash_after_results_install=True))
        self.settle()
        attempt = self.task_attempts()["sample"]
        result = self.work / "results/sample/result"
        inode = result.stat().st_ino
        (self.work / "extra.txt").unlink()
        damaged = result.with_suffix(".missing")
        result.rename(damaged)
        self.assertIn("extra.txt", self.cli("run", success=False))
        damaged.rename(result)
        evidence = self.snapshot()
        self.assertIn("finish checked installed", self.cli("run", "--dry-run"))
        self.assertEqual(self.snapshot(), evidence)
        self.cli("run")
        self.settle()
        self.assertIn("sample: reusable", self.cli("wait", "sample"))
        self.assertEqual(self.task_attempts()["sample"], attempt)
        self.assertEqual(result.stat().st_ino, inode)

    def test_compaction_retry_uses_checked_completion_without_logical_sources(self):
        self.cli("-b", "recovery_fixture", "run",
                 env=self.fault("gwflow_complete", compaction_fault="after-consolidating"))
        self.settle()
        attempt = self.task_attempts()["sample"]
        (self.work / "extra.txt").unlink()
        self.assertIn("record compaction unfinished", self.cli("run", "--dry-run"))
        self.cli("run")
        self.settle()
        self.assertIn("sample: reusable", self.cli("wait", "sample"))
        self.assertEqual(self.task_attempts()["sample"], attempt)

    def test_descriptor_binding_requires_a_target_input_from_its_own_task(self):
        workflow = self.work / "workflow.py"
        declaration = workflow.read_text()
        for source, replacement, error in [
            ('inputs=[pair, "decode.py"]', 'inputs=["decode.py"]', "not a declared target input"),
            ('inputs=[pair, "decode.py"]', 'inputs=["input.txt", "decode.py"]', "undeclared Task boundary"),
            ('inputs=[pair, "decode.py"]', 'inputs=[Task([]).logical_input("pair", identity="sample-1", '
             'files={"left": "input.txt"}, metadata={}), "decode.py"]', "must belong to"),
        ]:
            with self.subTest(error=error):
                workflow.write_text(declaration.replace(source, replacement))
                self.assertIn(error, self.cli("run", success=False))
                self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_container_plans_descriptor_and_ordinary_input_overrides(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(textwrap.dedent("""\
            from gwflow import Task, Workflow, shell, task_template

            @task_template
            def sample():
                task = Task(inputs=["decode.py"])
                seed = task.target("seed", inputs=[], outputs=["label"])
                seed << "echo a-label > label"
                label = seed.output("label")
                pair = task.logical_input("pair", identity=1, metadata={},
                                          files={"../left": "input.txt", "right/part": "extra.txt"})
                decode = task.target("decode", inputs=[pair, "decode.py", label], outputs=["decoded"],
                                     image="local.sif", stage_as={"docs/pair.json": pair,
                                     "scripts/decoder": "decode.py", "seed/label": label})
                decode << shell("python {decoder} {descriptor} {label} > decoded",
                                decoder="decode.py", descriptor=pair, label=label)
                task.retain("result", source=decode.output("decoded"), path="result")
                return task

            gwf = Workflow()
            gwf.task(sample())
        """))
        (self.work / "local.sif").touch()
        self.assertIn("Would submit 4 jobs", self.cli("run", "--dry-run"))
        self.cli("run")
        self.settle()
        # The empty image (or an absent Apptainer) must fail at launch. A
        # declaration or staging error would prevent reaching this diagnostic.
        self.assertIn(f"Task sample target decode image {self.work / 'local.sif'}",
                      self.cli("logs", "sample__decode", "--stderr", "--no-pager"))
        descriptor, = (self.work / "work/sample").rglob("staging/docs/pair.json")
        files = json.loads(descriptor.read_text())["files"]
        for key, source in (("../left", "input.txt"), ("right/part", "extra.txt")):
            self.assertTrue(Path(files[key]).is_symlink())
            self.assertEqual(Path(files[key]).resolve(), self.work / source)
        self.assertEqual((descriptor.parents[1] / "scripts/decoder").resolve(), self.work / "decode.py")
        self.assertEqual((descriptor.parents[1] / "seed/label").read_text(), "a-label\n")
        self.assertFalse((self.work / "results/sample").exists())

    def test_container_descriptor_collisions_identify_the_path_before_submission(self):
        workflow = self.work / "workflow.py"
        declaration = workflow.read_text()
        for layout, path in (
                ('{"pair.json": "decode.py"}', "pair.json"),
                ('{"decoded/descriptor.json": pair}', "decoded"),
                ('{"scripts": pair, "scripts/decode": "decode.py"}', "scripts"),
        ):
            with self.subTest(layout=layout):
                workflow.write_text(declaration.replace(
                    'outputs=["decoded"]', f'outputs=["decoded"], image="local.sif", stage_as={layout}'))
                output = self.cli("run", success=False)
                self.assertIn(path, output)
                self.assertIn("stage_as", output)
                self.assertNoSubmissions(output)
                self.assertFalse((self.work / ".gwf/gwflow").exists())

    @unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
    def test_container_retry_uses_the_frozen_members_after_restoration(self):
        workflow = self.work / "workflow.py"
        image = str(Path(os.environ["GWFLOW_TEST_SIF"]).resolve())
        workflow.write_text(workflow.read_text().replace(
            'outputs=["decoded"]', f'outputs=["decoded"], image={image!r}'))
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_baseline=True))
        self.wait_for(lambda: (self.work / "baseline-held").exists())
        source, away = self.work / "extra.txt", self.work / "away"
        try:
            attempt = self.task_attempts()["sample"]
            source.rename(away)
        finally:
            (self.work / "baseline-release").touch()
        self.settle()
        self.assertIn("extra.txt", self.cli("logs", "sample__decode", "--stderr", "--no-pager"))
        self.assertFalse((self.work / "results/sample").exists())
        blocked = self.cli("run", success=False)
        self.assertIn("extra.txt", blocked)
        self.assertNoSubmissions(blocked)
        away.rename(source)
        retried = self.cli("run", "sample", "--details")
        for local in ("decode", "keep", "gwflow_complete"):
            self.assertIn(f"sample__{local}: confirmed submitted", retried)
        self.assertIn("All awaited Task attempts are complete.",
                      self.cli("wait", "sample", "--interval", "0.2", "--timeout", "20"))
        self.assertEqual(self.task_attempts()["sample"], attempt)
        self.assertEqual((self.work / "results/sample/result").read_text(), "sample-1\nhello:extra\n")

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
