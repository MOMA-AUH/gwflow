"""Retry and completion recovery through the installed CLI and local workers."""

import json
import os
import re
import shutil
import subprocess
import unittest

from gwf.backends.local import Client, LocalStatus

import test_reuse


class RecoveryCliTests(test_reuse.LocalBackendTestCase):
    workers = 4

    def configure_resource_workflow(self, *, completion_defaults=None):
        configuration = "" if completion_defaults is None else f", completion_defaults={completion_defaults!r}"
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow(defaults={'cores': 3, 'memory': '8g'}"
            + configuration + ")\n"
            "task = Task(inputs=['input.txt'], outputs=['result.txt'], defaults={'cores': 7})\n"
            "task.target('prepare', inputs=['input.txt'], outputs=['middle.txt'], memory='4g') << 'cp input.txt middle.txt'\n"
            "task.target('finish', inputs=['middle.txt'], outputs=['result.txt']) << 'cp middle.txt result.txt'\n"
            "gwf.task_from_template('text', task)\n"
            "gwf.target('standalone', inputs=[], outputs=['standalone.txt'], cores=5) << 'touch standalone.txt'\n"
        )

    def submitted_options(self):
        return {
            item["name"]: item["options"]
            for item in map(json.loads, (self.work / "submitted-options.jsonl").read_text().splitlines())
        }

    def test_completion_options_inherit_workflow_defaults_and_apply_overrides(self):
        self.configure_resource_workflow(completion_defaults={"cores": 1, "memory": None})
        env = self.inject(capture_options=True)
        self.cli("-b", "recovery_fixture", "run", env=env)
        self.finish()
        options = self.submitted_options()
        self.assertEqual(options["text__gwflow_complete"]["cores"], 1)
        self.assertNotIn("memory", options["text__gwflow_complete"])
        self.assertEqual(options["text__prepare"]["cores"], 7)
        self.assertEqual(options["text__prepare"]["memory"], "4g")
        self.assertEqual(options["standalone"]["cores"], 5)

        (self.work / "middle.txt").unlink()
        self.configure_resource_workflow(completion_defaults={"cores": 2})
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertFalse((self.work / "middle.txt").exists())

    def test_omitted_completion_overrides_and_rejected_submission_recover(self):
        self.configure_resource_workflow()
        env = self.inject(capture_options=True, reject="text__gwflow_complete")
        output = self.cli("-b", "recovery_fixture", "run", success=False, env=env)
        self.assertIn("injected submission failure", output)
        self.assertEqual(self.submitted_options()["text__gwflow_complete"]["cores"], 3)
        self.assertEqual(self.submitted_options()["text__gwflow_complete"]["memory"], "8g")
        self.wait_for(lambda: (self.work / "result.txt").exists())
        records = self.records("text")
        self.assertNotIn(json.loads(records["expected.json"])["attempt"] + ".json", records)
        output = self.cli("run")
        self.assertIn("Submitted target text__gwflow_complete", output)
        self.assertNotIn("Submitted target text__prepare", output)
        self.finish()

    def configure_workflow(self, command="old"):
        shutil.copy(test_reuse.FIXTURES / "retry_tasks.py", self.work)
        (self.work / "definition.json").write_text(json.dumps(command))
        (self.work / "workflow.py").write_text(
            "import json\n"
            "from pathlib import Path\n"
            "from gwflow import Workflow\n"
            "from retry_tasks import retry_task, unrelated_task\n"
            "Path('workflow-loaded').touch()\n"
            "gwf = Workflow()\n"
            "gwf.task_from_template('retry', retry_task(json.loads(Path('definition.json').read_text())))\n"
            "gwf.task_from_template('unrelated', unrelated_task())\n"
        )

    def records(self, task="retry"):
        return {
            path.name: path.read_bytes()
            for path in (self.work / ".gwf" / "gwflow" / task).glob("*.json")
        }

    def release(self, *names):
        for name in names:
            (self.work / f"{name}.release").touch()

    def started(self, *names):
        self.wait_for(lambda: all((self.work / f"{name}.started").exists() for name in names))

    def settle(self):
        # Historical failed jobs remain in the worker's status map after retry.
        def done():
            with Client.connect(port=self.port) as client:
                states = client.status()
            return states and not any(
                state in (LocalStatus.SUBMITTED, LocalStatus.RUNNING)
                for state in states.values()
            )
        self.wait_for(done)

    def completed_setup(self):
        (self.work / "allow-work").touch()
        self.release("old", "sibling", "unrelated")
        self.run_complete()

    def assert_reused(self):
        (self.work / "sibling.tmp").unlink()
        records, trace = self.records(), self.trace()
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(self.records(), records)
        self.assertEqual(self.trace(), trace)
        self.assertFalse((self.work / "sibling.tmp").exists())

    def explain_before_run(self):
        records = {name: self.records(name) for name in ("retry", "unrelated")}
        trace = self.trace()
        output = self.cli("explain", "--details")
        self.assertNotIn("Submitted target", output)
        self.assertEqual({name: self.records(name) for name in records}, records)
        self.assertEqual(self.trace(), trace)
        self.assertEqual(set(re.findall(r"Would submit (\w+)", output)),
                         set(re.findall(r"Would submit (\w+)", self.cli("run", "--dry-run"))))
        return output

    def inject(self, **options):
        shutil.copy(test_reuse.FIXTURES / "recovery_backend.py", self.work)
        metadata = self.work / "recovery_backend-1.0.dist-info"
        metadata.mkdir(exist_ok=True)
        (metadata / "entry_points.txt").write_text(
            "[gwf.backends]\nrecovery_fixture = recovery_backend:setup\n"
        )
        self.configure(**{"backend.recovery_fixture.port": self.port})
        (self.work / "injection.json").write_text(json.dumps(options))
        return {**os.environ, "PYTHONPATH": str(self.work)}

    def launch(self, env):
        process = subprocess.Popen(
            [test_reuse.GWF, "-b", "recovery_fixture", "run"], cwd=self.work,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_worker, process)
        return process

    def test_overlapping_invocations_wait_for_cli_tracking_then_respect_active_jobs(self):
        self.configure(use_spec_hashes=True)
        (self.work / "allow-work").touch()
        first = self.launch(self.inject(hold_tracking=True))
        self.wait_for(lambda: (self.work / "tracking-held").exists())
        self.started("old", "sibling", "unrelated")
        records = self.records()
        (self.work / "workflow-loaded").unlink()
        second = self.launch(self.inject())
        self.wait_for(lambda: (self.work / "workflow-loaded").exists())
        try:
            second.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            (self.work / "tracking-release").touch()
        for process in (first, second):
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, out + err)
            if process is second:
                self.assertNotIn("Submitted target", out + err)
        self.assertEqual(self.records(), records)
        self.assertEqual(self.trace(), {"work": 1})
        self.release("old", "sibling", "unrelated")
        self.finish()

    def test_retry_failed_target_while_sibling_and_unrelated_task_are_active(self):
        self.cli("run")
        self.started("sibling", "unrelated")
        def failed():
            with Client.connect(port=self.port) as client:
                return LocalStatus.FAILED in client.status().values()
        self.wait_for(failed)
        records, unrelated = self.records(), self.records("unrelated")
        (self.work / "allow-work").touch()
        plan = self.explain_before_run()
        self.assertIn("failed", plan)
        self.assertIn("running", plan)
        self.assertRegex(plan, r"retry__work: Current: backend failed; Planned: retry")
        self.assertRegex(plan, r"retry__sibling: Current: backend running; Planned: active work left alone")
        self.assertEqual(set(re.findall(r"Would submit (\w+)", plan)), {"retry__work"})
        self.assertIn("Recovery:", plan)
        self.assertIn("later ordinary invocation", plan)
        output = self.cli("run")
        self.assertIn("Submitted target retry__work", output)
        self.assertNotIn("Submitted target retry__sibling", output)
        self.assertNotIn("Submitted target unrelated__", output)
        self.started("old")
        self.assertNotEqual(self.records(), records)
        self.assertEqual(self.records("unrelated"), unrelated)
        records = self.records()
        plan = self.explain_before_run()
        self.assertRegex(plan, r"Recovery:.*retry__gwflow_complete.*later ordinary invocation.*may be needed")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(self.records(), records)
        self.assertEqual(self.trace(), {"work": 2})
        # The already queued finalizer still depends on the failed attempt.
        # Let it settle; ordinary gwf can recover it on a later invocation.
        self.release("old", "sibling", "unrelated")
        self.settle()
        plan = self.explain_before_run()
        self.assertIn("completion-only repair", plan)
        self.assertEqual(set(re.findall(r"Would submit (\w+)", plan)), {"retry__gwflow_complete"})
        self.assertNotIn("Recovery:", plan)
        output = self.cli("run")
        self.assertIn("Submitted target retry__gwflow_complete", output)
        self.assertNotIn("Submitted target retry__work", output)
        self.settle()
        self.assertEqual(self.trace(), {"work": 2, "sibling": 1, "unrelated": 1})
        self.assert_reused()

    def test_old_definition_jobs_and_obsolete_finalizer_require_later_run(self):
        self.configure(use_spec_hashes=True)
        (self.work / "allow-work").touch()
        self.cli("run")
        self.started("old", "sibling", "unrelated")
        self.assertNotIn("Recovery:", self.explain_before_run())
        old_expected = self.records()["expected.json"]
        self.configure_workflow(command="new")
        plan = self.explain_before_run()
        self.assertIn("Current: not reusable", plan)
        self.assertIn("no new submissions", plan)
        self.assertNotIn("Would submit", plan)
        self.assertRegex(plan, r"Recovery:.*retry__gwflow_complete")
        self.assertIn("later ordinary invocation", plan)
        self.assertNotIn("Submitted target", self.cli("run"))
        pending = self.records()
        self.assertNotEqual(pending["expected.json"], old_expected)
        self.assertIn("later ordinary invocation", self.explain_before_run())
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(self.records(), pending)
        self.release("old", "sibling", "unrelated")
        self.finish()
        published = self.records()
        self.assertIn(old_expected, [data for name, data in published.items() if name != "expected.json"])
        self.assertNotIn(pending["expected.json"], [data for name, data in published.items() if name != "expected.json"])
        plan = self.explain_before_run()
        self.assertEqual(set(re.findall(r"Would submit (\w+)", plan)),
                         {"retry__work", "retry__gwflow_complete"})
        self.assertNotIn("Recovery:", plan)
        output = self.cli("run")
        self.assertIn("Submitted target retry__work", output)
        self.assertNotIn("Submitted target retry__sibling", output)
        self.started("new")
        self.release("new")
        self.finish()
        self.assertEqual((self.work / "result.txt").read_text(), "new\n")
        self.assertEqual(self.trace(), {"work": 2, "sibling": 1, "unrelated": 1})
        for name, data in published.items():
            if name != "expected.json":
                self.assertEqual(self.records()[name], data)
        self.assert_reused()

    def test_marker_only_recovery_keeps_missing_path_without_tracking(self):
        self.completed_setup()
        records = self.records()
        for name in records:
            if name != "expected.json":
                (self.work / ".gwf" / "gwflow" / "retry" / name).unlink()
        output = self.run_complete()
        self.assertIn("Submitted target retry__gwflow_complete", output)
        self.assertNotIn("Submitted target retry__work", output)
        self.assertEqual(self.records(), records)
        self.assert_reused()

    def test_unusable_records_get_new_paths_without_tracking(self):
        self.completed_setup()
        directory = self.work / ".gwf" / "gwflow" / "retry"
        for damaged in ("published", "expected"):
            for content in (b"not JSON", self.records("unrelated")["expected.json"]):
                with self.subTest(damaged=damaged, content=content):
                    records = self.records()
                    name = "expected.json" if damaged == "expected" else next(
                        name for name, data in records.items()
                        if name != "expected.json" and data == records["expected.json"]
                    )
                    (directory / name).write_bytes(content)
                    damaged_records = self.records()
                    output = self.run_complete()
                    self.assertIn("Submitted target retry__gwflow_complete", output)
                    self.assertNotIn("Submitted target retry__work", output)
                    after = self.records()
                    self.assertNotEqual(after["expected.json"], records["expected.json"])
                    self.assertTrue(set(after) - set(records))
                    for old_name, data in damaged_records.items():
                        if old_name != "expected.json":
                            self.assertEqual(after[old_name], data)
        self.assertEqual(self.trace(), {"work": 1, "sibling": 1, "unrelated": 1})
        self.assert_reused()

    def test_partial_submission_failure_recovers_while_submitted_jobs_run(self):
        (self.work / "allow-work").touch()
        env = self.inject(reject="retry__work")
        output = self.cli("-b", "recovery_fixture", "run", success=False, env=env)
        self.assertIn("injected submission failure", output)
        self.started("sibling")
        before = self.records()
        output = self.cli("run")
        self.assertIn("Submitted target retry__work", output)
        self.assertNotIn("Submitted target retry__sibling", output)
        self.assertNotEqual(self.records(), before)
        self.started("old", "unrelated")
        self.release("old", "sibling", "unrelated")
        self.finish()
        self.assertEqual(self.trace(), {"work": 1, "sibling": 1, "unrelated": 1})
        self.assert_reused()

    def test_current_finalizer_cannot_hide_active_old_command_hashes(self):
        self.configure(use_spec_hashes=True)
        (self.work / "allow-work").touch()
        env = self.inject(reject="retry__gwflow_complete")
        self.cli("-b", "recovery_fixture", "run", success=False, env=env)
        self.started("old", "sibling")
        self.configure_workflow(command="new")
        plan = self.explain_before_run()
        self.assertIn("Would submit retry__gwflow_complete", plan)
        self.assertNotIn("Would submit retry__work", plan)
        self.assertRegex(plan, r"Recovery:.*retry__work.*command")
        self.assertIn("later ordinary invocation", plan)
        output = self.cli("run")
        self.assertNotIn("Submitted target retry__work", output)
        self.assertIn("Submitted target retry__gwflow_complete", output)
        pending = self.records()["expected.json"]
        self.release("old", "sibling", "unrelated")
        self.finish()
        self.assertIn(pending, [data for name, data in self.records().items() if name != "expected.json"])
        self.assertEqual((self.work / "result.txt").read_text(), "old\n")
        output = self.cli("run")
        self.assertIn("Submitted target retry__work", output)
        self.started("new")
        self.release("new")
        self.finish()
        self.assertEqual((self.work / "result.txt").read_text(), "new\n")
        self.assert_reused()

    def test_interrupted_before_submission_recovers_and_releases_guard(self):
        (self.work / "allow-work").touch()
        first = self.launch(self.inject(hold_submission="retry__sibling"))
        self.wait_for(lambda: (self.work / "submission-held").exists())
        records = self.records()
        self.assertTrue(records)
        first.kill()
        first.communicate(timeout=10)
        # A leftover temporary publication must never count as completion.
        directory = self.work / ".gwf" / "gwflow" / "retry"
        (directory / ".pending-interrupted").write_text('{"schema":')
        self.release("old", "sibling", "unrelated")
        output = self.run_complete()
        self.assertIn("Submitted target retry__work", output)
        self.assertNotEqual(self.records()["expected.json"], records["expected.json"])
        self.assertEqual(self.trace(), {"work": 1, "sibling": 1, "unrelated": 1})
        self.assert_reused()

    def test_job_failure_between_planning_and_submission_recovers_on_later_run(self):
        (self.work / "allow-work").touch()
        (self.work / "fail-after-release").touch()
        self.cli("run")
        self.started("old", "sibling", "unrelated")
        env = self.inject(fail_after_observation="retry__work")
        output = self.cli("-b", "recovery_fixture", "run", env=env)
        self.assertIn("Submitted target retry__work", output)
        self.assertNotIn("Submitted target retry__sibling", output)
        self.assertNotIn("Submitted target retry__gwflow_complete", output)
        self.release("sibling", "unrelated")
        self.settle()
        self.assertIn("Submitted target retry__gwflow_complete", self.cli("run"))
        self.settle()
        self.assertEqual(self.trace(), {"work": 2, "sibling": 1, "unrelated": 1})
        self.assert_reused()


if __name__ == "__main__":
    unittest.main()
