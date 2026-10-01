"""Fresh Task attempts, force propagation, and recoverable initialization."""

from collections import Counter
import json
import os
import shlex
import shutil
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_managed
import test_managed_recovery


class FreshAttemptTests(LocalBackendTestCase):
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject
    def configure_workflow(self):
        trace = shlex.quote(str(self.work / "trace"))
        declaration = "from gwflow import Task, Workflow, shell\ngwf = Workflow()\n"
        for name in ("a", "b"):
            command = f"echo {name} >> {trace}; printf {name} > private.txt; touch -m -d @946684800 private.txt"
            declaration += (
                "task = Task(inputs=[])\n"
                "target = task.target('compute', inputs=[], outputs=['private.txt'])\n"
                f"target << {command!r}\n"
                "task.retain('value', source=target.output('private.txt'), path='result.txt')\n"
                f"{name} = gwf.task_from_template({name!r}, task)\n"
            )
        declaration += (
            "task = Task(inputs=[a.outputs['value']])\n"
            "target = task.target('compute', inputs=task.inputs, outputs=['copy.txt'])\n"
            f"target << shell({'echo c >> ' + trace + '; cat {source} > {out}'!r}, source=a.outputs['value'], out=target.output('copy.txt'))\n"
            "task.retain('value', source=target.output('copy.txt'), path='result.txt')\n"
            "gwf.task_from_template('c', task)\n"
        )
        (self.work / "workflow.py").write_text(declaration)

    def attempts(self):
        return {name: next(line for line in self.cli("explain", "--details", name).splitlines() if "Attempt:" in line)
                for name in ("a", "b", "c")}

    def test_selected_force_refreshes_consumers_even_with_identical_metadata(self):
        self.check_selected_force()

    def test_producer_identity_invalidates_consumers_with_command_tracking(self):
        self.configure(use_spec_hashes=True)
        self.check_selected_force()

    def check_selected_force(self):
        self.run_complete()
        before = self.attempts()
        result = self.work / "results/a/result.txt"
        info = result.stat()
        preview = self.cli("explain", "--force-task", "a")
        self.assertIn("Task a: fresh;", preview)
        self.assertIn("Task b: reuse;", preview)
        self.assertIn("Task c: fresh;", preview)
        self.assertIn("remove previous results", preview)
        self.cli("run", "--force-task", "a", "--dry-run")
        self.assertEqual(self.attempts(), before)
        self.assertEqual(result.read_text(), "a")
        self.cli("run", "--force-task", "a")
        self.finish()
        after = self.attempts()
        self.assertNotEqual(after["a"], before["a"])
        self.assertEqual(after["b"], before["b"])
        self.assertNotEqual(after["c"], before["c"])
        self.assertEqual((result.stat().st_size, result.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_untracked_runtime_package_parameter_and_environment_need_force(self):
        self.configure(use_spec_hashes=True)
        package = self.work / "task_package.py"
        parameter = self.work / "hidden_parameter"
        environment = self.work / "runtime_environment"
        package.write_text("from pathlib import Path\nimport os, sys\nprint('v1', Path(sys.argv[1]).read_text(), os.environ['TASK_MODE'])\n")
        parameter.write_text("first")
        environment.write_text("export TASK_MODE=old\n")
        command = (f". {shlex.quote(str(environment))}; {shlex.quote(sys.executable)} "
                   f"{shlex.quote(str(package))} {shlex.quote(str(parameter))} > out.txt")
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow\ngwf = Workflow()\n"
            "task = Task(inputs=[])\n"
            "target = task.target('compute', inputs=[], outputs=['out.txt'])\n"
            f"target << {command!r}\n"
            "task.retain('value', source=target.output('out.txt'), path='out.txt')\n"
            "gwf.task_from_template('runtime', task)\n"
        )
        self.run_complete()
        result = self.work / "results/runtime/out.txt"
        self.assertEqual(result.read_text(), "v1 first old\n")
        package.write_text(package.read_text().replace("'v1'", "'version2'"))
        parameter.write_text("second")
        environment.write_text("export TASK_MODE=new\n")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(result.read_text(), "v1 first old\n")
        self.cli("run", "--force-task", "runtime")
        self.finish()
        self.assertEqual(result.read_text(), "version2 second new\n")

    def interrupted_initialization(self, phase):
        self.run_complete()
        old = self.attempts()
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "a", "run", "--force-task", "a"],
                                cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertIn(result.returncode, (92, 93, 94), result.stdout + result.stderr)
        pending = self.attempts()
        self.assertNotEqual(pending["a"], old["a"])
        self.cli("run")
        self.finish()
        self.assertEqual(self.attempts()["a"], pending["a"])
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_initialization_before_result_removal_resumes_selected_attempt(self):
        self.interrupted_initialization("before_removal")

    def test_initialization_during_result_removal_resumes_selected_attempt(self):
        self.interrupted_initialization("during_removal")

    def test_ready_initialization_without_admissions_keeps_selected_attempt(self):
        self.interrupted_initialization("after_ready")

    def test_changed_input_metadata_starts_fresh_producer_and_consumer(self):
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("Task(inputs=[])", "Task(inputs=['input.txt'])", 1))
        self.run_complete()
        before = self.attempts()
        source = self.work / "input.txt"
        info = source.stat()
        source.write_text("different size\n")
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertIn("Task a: fresh;", self.cli("explain"))
        self.cli("run")
        self.finish()
        self.assertNotEqual(self.attempts()["a"], before["a"])
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_rejected_admission_leaves_old_results_absent_and_resumes(self):
        self.run_complete()
        self.cli("-b", "recovery_fixture", "run", "--force-task", "a", env=self.inject(reject_before_admission=True), success=False)
        pending = self.attempts()
        self.assertFalse((self.work / "results/a").exists())
        self.assertFalse((self.work / "results/c").exists())
        self.assertEqual((self.work / "results/b/result.txt").read_text(), "b")
        self.cli("run")
        self.finish()
        self.assertEqual(self.attempts(), pending)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_invalid_force_selectors_preserve_previous_results(self):
        self.run_complete()
        before = self.attempts()
        for arguments in (("run", "--force", "--force-task", "a"), ("explain", "--force", "--force-task", "a"),
                          ("run", "--force-task", "missing"), ("run", "--force-task", "a__compute"),
                          ("run", "a"), ("run", "--group", "anything"), ("run", "--no-deps")):
            with self.subTest(arguments=arguments):
                self.cli(*arguments, success=False)
                self.assertEqual(self.attempts(), before)
                self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")

    def test_active_consumer_prevents_forcing_producer_after_declaration_removal(self):
        self.run_complete()
        held, release = (self.work / name for name in ("held", "release"))
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        wait = f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
        workflow.write_text(original.replace("echo c", wait + "echo c"))
        self.cli("run", "--force-task", "c")
        self.wait_for(held.exists)
        try:
            pending = self.attempts()["c"]
            self.assertIn("work blocks replacement", self.cli("run", "--force-task", "c", success=False))
            self.assertEqual(self.attempts()["c"], pending)
            workflow.write_text(original.split("task = Task(inputs=[a.outputs")[0])
            self.assertIn("Active consumers block replacement: c", self.cli("run", "--force-task", "a", success=False))
            self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
            self.assertEqual(Counter((self.work / "trace").read_text().splitlines())["a"], 1)
        finally:
            release.touch()
        self.finish()

    def test_damaged_producer_results_defer_consumer_without_forcing(self):
        self.run_complete()
        before = self.attempts()
        (self.work / "results/a/result.txt").unlink()
        self.assertIn("Task c: deferred;", self.cli("explain"))
        self.run_complete()
        self.assertEqual(self.attempts(), before)
        self.assertIn("Task c: reuse;", self.cli("explain"))
        self.assertTrue((self.work / "work/a").exists())
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "a")

    def test_crash_before_initialization_intent_preserves_old_results(self):
        self.run_complete()
        before = self.attempts()
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "before_initialization_intent", "a", "run", "--force-task", "a"],
                                cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 91, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), before)
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        self.cli("run", "--force-task", "a")
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_first_initialization_interrupted_before_intent_can_restart(self):
        self.first_initialization_interrupted("before_initialization_intent", 91)

    def test_first_initialization_interrupted_before_selection_can_restart(self):
        self.first_initialization_interrupted("before_selection", 90)

    def first_initialization_interrupted(self, phase, code):
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "a", "run"],
                                cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertFalse((self.work / "results/a").exists())
        self.assertIn("Task a: fresh;", self.cli("explain"))
        self.cli("run")
        self.finish()
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "a")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 1, "c": 1})

    def test_force_recreates_removed_work_root_before_removing_results(self):
        self.run_complete()
        before = self.attempts()
        shutil.rmtree(self.work / "work")
        self.assertIn("Task a: reuse;", self.cli("explain"))
        self.cli("run", "--force-task", "a", "--dry-run")
        self.assertFalse((self.work / "work").exists())
        self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
        self.cli("run", "--force-task", "a")
        self.finish()
        after = self.attempts()
        self.assertNotEqual(after["a"], before["a"])
        self.assertEqual(after["b"], before["b"])
        self.assertNotEqual(after["c"], before["c"])
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_work_root_recreation_interruptions_preserve_results_and_restart(self):
        self.run_complete()
        for phase, code in (("before_work_recreation", 95), ("before_work_install", 96), ("after_work_install", 97)):
            with self.subTest(phase=phase):
                before = self.attempts()
                shutil.rmtree(self.work / "work")
                result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), phase, "a", "run", "--force-task", "a"],
                                        cwd=self.work, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual((self.work / "results/a/result.txt").read_text(), "a")
                self.assertEqual(self.attempts(), before)
                self.cli("run", "--force-task", "a")
                self.finish()
                self.assertNotEqual(self.attempts()["a"], before["a"])
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 4, "b": 1, "c": 4})

    def test_changed_pending_structure_selects_another_fresh_attempt(self):
        self.run_complete()
        result = subprocess.run([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "before_removal", "a", "run", "--force-task", "a"],
                                cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        pending = self.attempts()["a"]
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("a = gwf.task_from_template", "task.retain('extra', source=target.output('private.txt'), path='extra.txt')\na = gwf.task_from_template"))
        self.cli("run")
        self.finish()
        self.assertNotEqual(self.attempts()["a"], pending)
        self.assertEqual((self.work / "results/a/extra.txt").read_text(), "a")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_result_symlink_substitution_cannot_redirect_initialization_removal(self):
        self.run_complete()
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "result.txt").write_text("untouched")
        process = subprocess.Popen([sys.executable, str(FIXTURES / "frontend_fault.py"), str(self.work), "gate_before_removal", "a", "run", "--force-task", "a"],
                                   cwd=self.work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: (self.work / "frontend-held").exists())
            (self.work / "results/a").rename(self.work / "owned-results-away")
            (self.work / "results/a").symlink_to(outside, target_is_directory=True)
        finally:
            (self.work / "frontend-release").touch()
            stdout, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual((outside / "result.txt").read_text(), "untouched")
        self.assertEqual((self.work / "owned-results-away/result.txt").read_text(), "a")
        self.assertTrue((self.work / "results/a").is_symlink())

    def test_unknown_result_ownership_blocks_all_requested_removal(self):
        self.run_complete()
        manifest = next((self.work / ".gwf/gwflow").glob("owners/*/tasks/b/attempts/*/**/manifest.json"))
        original = json.loads(manifest.read_text())
        missing_metadata = {key: value for key, value in original.items() if key != "outputs"}
        for contents in ("{truncated", json.dumps(missing_metadata)):
            with self.subTest(contents=contents):
                manifest.write_text(contents)
                self.assertIn("ownership", self.cli("run", "--force", success=False))
                for name, expected in (("a", "a"), ("b", "b"), ("c", "a")):
                    self.assertEqual((self.work / "results" / name / "result.txt").read_text(), expected)

    def test_force_all_and_repeatable_exact_task_selection(self):
        self.run_complete()
        self.cli("run", "--force")
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 2, "c": 2})
        self.cli("run", "--force-task", "b", "--force-task", "b")
        self.finish()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 3, "c": 2})

    def test_uncertain_submission_blocks_another_forced_attempt(self):
        self.run_complete()
        held, release = (self.work / name for name in ("held", "release"))
        workflow = self.work / "workflow.py"
        wait = f"touch {shlex.quote(str(held))}; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
        workflow.write_text(workflow.read_text().replace("echo a", wait + "echo a"))
        self.cli("-b", "recovery_fixture", "run", "--force-task", "a", env=self.inject(lose_tracking="a__compute"), success=False)
        self.wait_for(held.exists)
        try:
            pending = self.attempts()["a"]
            self.assertIn("unresolved submission", self.cli("run", "--force-task", "a", success=False))
            self.assertEqual(self.attempts()["a"], pending)
        finally:
            release.touch()
        self.settle()
        self.cli("run")
        self.settle()
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_reordering_and_resources_reuse_but_retained_mapping_changes_refresh(self):
        workflow = self.work / "workflow.py"
        extra = ("extra = task.target('extra', inputs=[], outputs=['one.txt', 'two.txt'])\n"
                 "extra << 'printf one > one.txt; printf two > two.txt'\n"
                 "task.retain('extra', source=extra.output('one.txt'), path='extras/one.txt')\n")
        workflow.write_text(workflow.read_text().replace("a = gwf.task_from_template", extra + "a = gwf.task_from_template"))
        self.configure(use_spec_hashes=True)
        self.run_complete()
        before = self.attempts()
        workflow.write_text(workflow.read_text().replace("Workflow()", "Workflow(defaults={'cores': 3})").replace(
            "a = gwf.task_from_template", "extra.outputs.reverse()\ntask.targets = dict(reversed(list(task.targets.items())))\ntask.retained = dict(reversed(list(task.retained.items())))\na = gwf.task_from_template"))
        self.cli("run")
        self.assertEqual(self.attempts(), before)
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 1, "b": 1, "c": 1})
        workflow.write_text(workflow.read_text().replace("a = gwf.task_from_template", "task.retain('second', source=extra.output('two.txt'), path='extras/two.txt')\na = gwf.task_from_template"))
        self.cli("run")
        self.finish()
        self.assertEqual((self.work / "results/a/extras/two.txt").read_text(), "two")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 2})

    def test_enabled_command_changes_refresh_and_disabling_tracking_reuses(self):
        self.configure(use_spec_hashes=True)
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("printf a >", "printf updated >"))
        self.cli("run")
        self.finish()
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "updated")
        before = self.attempts()
        self.configure(use_spec_hashes=False)
        workflow.write_text(workflow.read_text().replace("printf updated >", "printf third >"))
        self.cli("run")
        self.assertEqual(self.attempts(), before)
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "updated")

    def test_failed_fresh_computation_does_not_restore_old_results(self):
        self.run_complete()
        workflow = self.work / "workflow.py"
        original = workflow.read_text()
        workflow.write_text(original.replace("printf a >", "exit 17; printf a >"))
        self.cli("run", "--force-task", "a")
        self.settle()
        self.assertFalse((self.work / "results/a").exists())
        self.assertFalse((self.work / "results/c").exists())
        self.assertEqual((self.work / "results/b/result.txt").read_text(), "b")
        workflow.write_text(original)
        self.cli("run")
        self.settle()
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "a")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 3, "b": 1, "c": 2})

    def test_force_after_lost_acknowledgement_does_not_copy_old_admissions(self):
        self.cli("-b", "recovery_fixture", "run", env=self.inject(lose_ack="a__compute"), success=False)
        self.settle()
        self.cli("-b", "recovery_fixture", "run", "--force-task", "a", env=self.inject(reject_before_admission=True), success=False)
        self.cli("run")
        self.finish()
        self.assertEqual((self.work / "results/c/result.txt").read_text(), "a")
        self.assertEqual(Counter((self.work / "trace").read_text().splitlines()), {"a": 2, "b": 1, "c": 1})
