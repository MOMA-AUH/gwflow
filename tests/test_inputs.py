"""External input preparation through installed workflows and local workers."""

import json
import os
import shutil

from support import FIXTURES, LocalBackendTestCase
import test_managed
import test_managed_recovery


class ExternalInputTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject

    def configure_workflow(self, *, source="input.txt", boundary=None, settings="", command=None):
        if boundary is None:
            boundary = [source]
        if command is None:
            command = "printf '%s\\n' {source} > alias.txt; cat {source} > {out}"
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow, shell\n"
            f"gwf = Workflow({settings})\n"
            f"task = Task(inputs={boundary!r})\n"
            f"target = task.target('read', inputs={[source]!r}, outputs=['out.txt', 'alias.txt'])\n"
            f"target << shell({command!r}, source={source!r}, out=target.output('out.txt'))\n"
            "task.retain('result', source=target.output('out.txt'), path='result.txt')\n"
            "task.retain('alias', source=target.output('alias.txt'), path='alias.txt')\n"
            "gwf.task_from_template('sample', task)\n"
        )

    def test_external_alias_is_absolute_read_in_place_and_reusable(self):
        alias = self.work / "input's $alias.txt"
        alias.symlink_to("input.txt")
        self.configure_workflow(source=alias.name)
        before = (self.work / "input.txt").read_bytes()
        for command in (("explain",), ("status",), ("run", "--dry-run")):
            self.cli(*command)
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.run_complete()
        result = self.work / "results/sample"
        self.assertEqual((result / "result.txt").read_bytes(), before)
        self.assertEqual((result / "alias.txt").read_text(), str(alias) + "\n")
        self.assertEqual((self.work / "input.txt").read_bytes(), before)
        self.assertFalse(any(path.is_symlink() for path in (self.work / "work").rglob("*")))
        self.assertNotIn("Submitted target", self.cli("run"))

    def fault(self, job="gwflow_prepare", **options):
        shutil.copy(FIXTURES / "job_fault.py", self.work)
        (self.work / "job-fault.json").write_text(json.dumps(options))
        return self.inject(job_fault="sample__" + job)

    def test_submission_returns_before_preparation_and_observes_later_input(self):
        env = self.fault(gate_before_preparation=True)
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        try:
            self.assertFalse((self.work / "results/sample").exists())
            (self.work / "input.txt").write_text("changed before preparation\n")
        finally:
            (self.work / "preparation-release").touch()
        self.finish()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "changed before preparation\n")
        self.assertIn("reuse", self.cli("explain"))

    def test_changed_input_immediately_before_installation_prevents_completion(self):
        env = self.fault(job="gwflow_complete", gate_after_manifest=True)
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.wait_for(lambda: (self.work / "manifest-held").exists())
        try:
            (self.work / "input.txt").write_text("changed after computation\n")
        finally:
            (self.work / "manifest-release").touch()
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        self.assertIn("fresh attempt", self.cli("explain"))
        self.assertIn("changed after preparation", self.cli("logs", "sample__gwflow_complete", "--stderr", "--no-pager"))

    def test_interrupted_baseline_publication_can_restart_same_attempt(self):
        env = self.fault(crash_before_baseline=True)
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.settle()
        before = self.cli("explain", "--details")
        attempt_line = next(line for line in before.splitlines() if "Attempt:" in line)
        (self.work / "input.txt").write_text("baseline was not committed\n")
        self.assertIn("preparation", self.cli("run", "--dry-run"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "baseline was not committed\n")
        self.assertIn(attempt_line, self.cli("explain", "--details"))
        self.assertIn("reuse", self.cli("explain"))

    def test_retry_never_replaces_a_committed_baseline(self):
        env = self.fault(crash_after_baseline=True)
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.settle()
        evidence = {path: path.read_bytes() for path in (self.work / ".gwf/gwflow").rglob("*.json")}
        source = self.work / "input.txt"
        original, info = source.read_bytes(), source.stat()
        source.write_text("different input\n")
        self.assertIn("fresh attempt", self.cli("run", "--dry-run"))
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        source.write_bytes(original)
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_bytes(), original)
        self.assertIn("reuse", self.cli("explain"))
        # Fault injection intentionally locates a record by kind; user behavior
        # above establishes its meaning, this check enforces immutability.
        baselines = [path for path, content in evidence.items() if json.loads(content).get("kind") == "gwflow.inputs"]
        self.assertEqual(len(baselines), 1)
        self.assertEqual(baselines[0].read_bytes(), evidence[baselines[0]])

    def test_size_and_backward_mtime_changes_are_detected_without_preview_writes(self):
        self.run_complete()
        source = self.work / "input.txt"
        original, info = source.read_bytes(), source.stat()
        evidence = {path: path.read_bytes() for path in (self.work / ".gwf/gwflow").rglob("*.json")}
        for content, mtime in ((b"longer input\n", info.st_mtime_ns), (original, info.st_mtime_ns - 1_000_000_000)):
            with self.subTest(content=content, mtime=mtime):
                source.write_bytes(content)
                os.utime(source, ns=(info.st_atime_ns, mtime))
                self.assertRegex(self.cli("status"), r"Task sample\s+pending\s+0/1 target completed; fresh computation required")
                for command in (("explain",), ("status", "--details")):
                    self.assertIn("fresh attempt", self.cli(*command))
                self.assertIn("fresh attempt", self.cli("run", "--dry-run"))
                self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        # Deliberately document the accepted metadata-only detection limit.
        source.write_bytes(b"other\n")
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertIn("reuse", self.cli("explain"))
        self.assertEqual((self.work / "results/sample/result.txt").read_bytes(), original)

    def test_equal_metadata_symlink_retarget_after_preparation_blocks_computation(self):
        source = self.work / "input.txt"
        other = self.work / "other.txt"
        other.write_bytes(b"other\n")
        os.utime(other, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
        alias = self.work / "alias.txt"
        alias.symlink_to(source)
        self.configure_workflow(source=alias.name)
        self.cli("-b", "recovery_fixture", "run", env=self.fault(gate_after_baseline=True))
        self.wait_for(lambda: (self.work / "baseline-held").exists())
        try:
            alias.unlink()
            alias.symlink_to(other)
        finally:
            (self.work / "baseline-release").touch()
        self.settle()
        self.assertFalse((self.work / "results/sample").exists())
        self.assertFalse(list((self.work / "work").rglob("out.txt")))
        self.assertIn("fresh attempt", self.cli("explain"))
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/sample/result.txt").read_bytes(), b"other\n")

    def test_invalid_external_inputs_and_undeclared_bindings_fail_before_submission(self):
        (self.work / "directory").mkdir()
        os.mkfifo(self.work / "pipe")
        (self.work / "broken").symlink_to("missing")
        for source in ("missing", "directory", "pipe", "broken"):
            with self.subTest(source=source):
                self.configure_workflow(source=source)
                self.cli("run", success=False)
                self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.configure_workflow(boundary=[])
        self.assertIn("boundary input", self.cli("run", success=False))
        self.configure_workflow()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("source='input.txt'", "source='extra.txt'"))
        self.assertIn("declared target input", self.cli("run", success=False))

    def test_managed_storage_cannot_be_passed_as_external_input(self):
        for root in ("work", "results", ".gwf", "transfer"):
            directory = self.work / root
            directory.mkdir(exist_ok=True)
            source = directory / "managed.txt"
            source.write_text("untouched")
            alias = self.work / "external-alias"
            alias.symlink_to(source)
            try:
                for path in (str(source), alias.name):
                    self.configure_workflow(source=path, settings="results_staging_root='transfer'")
                    self.assertIn("managed storage", self.cli("run", success=False))
                    self.assertEqual(source.read_text(), "untouched")
            finally:
                alias.unlink()

    def test_preparation_resources_overlay_workflow_defaults(self):
        self.configure_workflow(settings="defaults={'cores':3, 'memory':'8g'}, preparation_defaults={'cores':1, 'memory':None}")
        self.cli("-b", "recovery_fixture", "run", env=self.inject(capture_options=True))
        self.finish()
        options = [json.loads(line) for line in (self.work / "submitted-options.jsonl").read_text().splitlines()]
        preparation = next(item["options"] for item in options if item["name"].startswith("sample__gwflow_prepare__"))
        compute = next(item["options"] for item in options if item["name"].startswith("sample__read__"))
        self.assertEqual(preparation, {"cores":1})
        self.assertEqual(compute, {"cores":3, "memory":"8g"})

    def test_disappeared_input_requires_fresh_attempt_without_changing_completion(self):
        self.run_complete()
        (self.work / "input.txt").unlink()
        self.assertIn("fresh attempt", self.cli("explain"))
        self.assertIn("fresh attempt", self.cli("run", success=False))
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "hello\n")

    def test_symlink_parent_components_preserve_the_declared_file(self):
        (self.work / "actual/nested").mkdir(parents=True)
        (self.work / "actual/input.txt").write_text("intended input\n")
        (self.work / "link").symlink_to("actual/nested")
        for source in ("link/../input.txt", str(self.work / "link/../input.txt")):
            with self.subTest(source=source):
                self.configure_workflow(source=source)
                self.run_complete()
                self.assertEqual((self.work / "results/sample/result.txt").read_text(), "intended input\n")
                self.assertEqual((self.work / "results/sample/alias.txt").read_text(), str(self.work / "link/../input.txt") + "\n")

    def test_preparation_retry_does_not_reuse_previous_admission_acknowledgement(self):
        self.cli("-b", "recovery_fixture", "run", env=self.fault(crash_before_baseline=True))
        self.settle()
        env = self.fault(gate_before_preparation=True)
        self.inject(job_fault="sample__gwflow_prepare", lose_tracking="sample__gwflow_prepare")
        self.cli("-b", "recovery_fixture", "run", env=env, success=False)
        self.wait_for(lambda: (self.work / "preparation-held").exists())
        try:
            self.assertIn("unresolved submission", self.cli("explain"))
            self.assertIn("unresolved submission", self.cli("run", success=False))
        finally:
            (self.work / "preparation-release").touch()
        self.settle()
