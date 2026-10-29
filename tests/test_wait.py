"""Observe existing Task attempts through the installed waiting command."""

import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import time
import shlex
import signal
import subprocess

from support import FIXTURES, LocalBackendTestCase
import test_fresh
import test_managed_recovery


class WaitTests(LocalBackendTestCase):
    inject = test_fresh.FreshAttemptTests.inject
    settle = test_fresh.FreshAttemptTests.settle
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow

    def test_already_complete_selected_tasks_return_immediately_without_submissions(self):
        self.run_complete('a')
        attempts = self.task_attempts()
        result = self.cli_result('wait', 'a')
        self.assertEqual(result.stdout, '')
        self.assertIn('Waiting', result.stderr)
        self.assertIn('complete', result.stderr)
        self.assertEqual(self.task_attempts(), attempts)
        self.assertFalse((self.work / 'results/b').exists())

    def start_wait(self, *args, env=None):
        output = (self.work / 'wait.stdout').open('w')
        errors = (self.work / 'wait.stderr').open('w')
        self.addCleanup(output.close)
        self.addCleanup(errors.close)
        process = subprocess.Popen([self.executable, *args], cwd=self.work, stdout=output, stderr=errors, env=env)
        self.addCleanup(self.stop_wait, process)
        return process

    def stop_wait(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=10)

    def wait_messages(self):
        return (self.work / 'wait.stderr').read_text()

    def gate_computation(self):
        workflow = self.work / 'workflow.py'
        gate = shlex.quote(str(self.work / 'release'))
        workflow.write_text(workflow.read_text().replace('echo a >>', f'while test ! -e {gate}; do sleep 0.05; done; echo a >>'))

    def block_loading_with_detached_child(self):
        workflow = self.work / 'workflow.py'
        child = ("import os, signal, time\nfrom pathlib import Path\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                 "Path('detached-pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
        workflow.write_text("import os, subprocess, sys, time\nfrom pathlib import Path\n"
                            "print('loading stdout', flush=True)\nprint('loading stderr', file=sys.stderr, flush=True)\n"
                            "os.write(1, b'loading descriptor\\n')\n"
                            "Path('inspection-root').write_text(str(Path(sys.pycache_prefix).parent))\n"
                            f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)\n"
                            "time.sleep(60)\n" + workflow.read_text())

    def test_active_work_reaches_completion_without_wait_submitting_jobs(self):
        self.gate_computation()
        self.cli('run', 'a')
        attempts = self.task_attempts()
        process = self.start_wait('wait', '--interval', '0.1', 'a')
        self.wait_for(lambda: 'a: running' in self.wait_messages())
        self.assertIsNone(process.poll(), self.wait_messages())
        (self.work / 'release').touch()
        self.assertEqual(process.wait(timeout=20), 0, self.wait_messages())
        self.assertEqual((self.work / 'wait.stdout').read_text(), '')
        self.assertEqual(self.task_attempts(), attempts)
        self.assertEqual((self.work / 'trace').read_text(), 'a\n')

    def test_timeout_caps_polling_pause_without_canceling_submitted_work(self):
        self.gate_computation()
        self.cli('run', 'a')
        attempts = self.task_attempts()
        process = self.start_wait('wait', '--interval', '60', '--timeout', '10', 'a')
        self.assertEqual(process.wait(timeout=18), 124, self.wait_messages())
        self.assertIn('a: running', self.wait_messages())
        self.assertIn('timed out', self.wait_messages())
        self.assertIn('may still be queued or running', self.wait_messages())
        self.assertEqual((self.work / 'wait.stdout').read_text(), '')
        (self.work / 'release').touch()
        self.finish()
        self.assertEqual(self.task_attempts(), attempts)
        self.assertEqual((self.work / 'trace').read_text(), 'a\n')

    def test_timeout_includes_blocked_configuration_loading(self):
        configuration = self.work / '.gwfconf.json'
        configuration.unlink()
        os.mkfifo(configuration)
        process = self.start_wait('wait', '--timeout', '10')
        writer = []

        def configuration_reader_started():
            try:
                writer.append(os.open(configuration, os.O_WRONLY | os.O_NONBLOCK))
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
                return False
            self.addCleanup(os.close, writer[0])
            return True

        self.wait_for(lambda: configuration_reader_started() or process.poll() is not None)
        self.assertTrue(writer, self.wait_messages())
        self.assertEqual(process.wait(timeout=15), 124, self.wait_messages())
        self.assertIn('timed out', self.wait_messages())
        self.assertEqual((self.work / 'wait.stdout').read_text(), '')

    def test_timeout_cleans_loading_descendants_and_preserves_captured_output(self):
        self.block_loading_with_detached_child()
        process = self.start_wait('wait', '--timeout', '10')
        self.wait_for(lambda: (self.work / 'detached-pid').exists() or process.poll() is not None)
        self.assertTrue((self.work / 'detached-pid').exists(), self.wait_messages())
        pid = int((self.work / 'detached-pid').read_text())
        root = Path((self.work / 'inspection-root').read_text())
        self.assertEqual(process.wait(timeout=15), 124, self.wait_messages())
        self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertFalse(root.exists())
        for message in ('loading stdout', 'loading stderr', 'loading descriptor', 'timed out'):
            self.assertIn(message, self.wait_messages())
        self.assertEqual((self.work / 'wait.stdout').read_text(), '')

    def test_timeout_interrupts_lock_and_scheduler_waits_and_releases_inspection_lock(self):
        self.gate_computation()
        self.cli('run', 'a')
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nimport os, sys\nfrom pathlib import Path\n"
                            "Path('inspection-pid').write_text(str(os.getpid()))\n"
                            "Path('inspection-root').write_text(str(Path(sys.pycache_prefix).parent))\n")
        with (self.work / '.gwf/gwflow-submission.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = self.start_wait('wait', '--timeout', '10', 'a')
            self.wait_for(lambda: 'Waiting for frontend' in self.wait_messages() or process.poll() is not None)
            self.assertEqual(process.wait(timeout=15), 124, self.wait_messages())
            self.assertIn('Waiting for frontend', self.wait_messages())
        # The wait process owns a fresh inspection for the blocked query. This
        # inspection acquires the frontend lock before contacting the backend.
        environment = self.inject(hold_observation=True)
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--timeout', '10', 'a', env=environment)
        self.wait_for(lambda: (self.work / 'observation-held').exists() or process.poll() is not None)
        self.assertTrue((self.work / 'observation-held').exists(), self.wait_messages())
        pid = int((self.work / 'inspection-pid').read_text())
        root = Path((self.work / 'inspection-root').read_text())
        self.assertEqual(process.wait(timeout=15), 124, self.wait_messages())
        self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertFalse(root.exists())
        with (self.work / '.gwf/gwflow-submission.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual((self.work / 'wait.stdout').read_text(), '')
        (self.work / 'release').touch()
        self.finish()
        self.assertEqual((self.work / 'trace').read_text(), 'a\n')

    def test_scheduler_failures_and_unknown_outcomes_do_not_restart_timeout(self):
        self.gate_computation()
        self.cli('run', 'a')
        for options, message in (({'fail_status_prefix': 'a__compute'}, 'Scheduler unavailable'),
                                 ({'job_states': {'a__compute': 'UNKNOWN'}}, 'unresolved submission')):
            with self.subTest(options=options):
                process = self.start_wait('-b', 'recovery_fixture', 'wait', '--timeout', '15',
                                          '--interval', '0.05', 'a', env=self.inject(**options))
                self.assertEqual(process.wait(timeout=22), 124, self.wait_messages())
                self.assertIn(message, self.wait_messages())
                self.assertIn('timed out', self.wait_messages())
                self.assertEqual(self.wait_messages().count(message), 1)
                self.assertEqual((self.work / 'wait.stdout').read_text(), '')
        (self.work / 'release').touch()
        self.finish()

    def test_completed_attempt_succeeds_within_limit_using_selected_workflow(self):
        self.run_complete('a')
        (self.work / 'workflow.py').rename(self.work / 'selected.py')
        result = self.cli_result('--file', 'selected.py:gwf', 'wait', 'a', '--timeout', '20')
        self.assertEqual(result.stdout, '')
        self.assertIn('All awaited Task attempts are complete', result.stderr)
        result = self.cli_result('--file', 'selected.py:gwf', 'wait', 'a', '--timeout', '1e-12', success=False)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn('timed out', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_invalid_timeout_values_are_usage_errors(self):
        for arguments in (('--timeout',), *(('--timeout', value) for value in ('0', '-1', 'nan', 'inf', '-inf', 'text'))):
            with self.subTest(arguments=arguments):
                result = self.cli_result('wait', *arguments, success=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('--timeout', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_pattern_scope_does_not_expand_when_new_matching_tasks_appear(self):
        self.gate_computation()
        self.cli('run', 'a')
        process = self.start_wait('wait', '--interval', '0.1', 'a*')
        self.wait_for(lambda: 'a: running' in self.wait_messages())
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace("alias='b'", "alias='another'"))
        (self.work / 'release').touch()
        self.assertEqual(process.wait(timeout=20), 0, self.wait_messages())
        self.assertNotIn('another:', self.wait_messages())
        self.assertFalse((self.work / 'results/another').exists())

    def test_unknown_job_outcome_keeps_polling_until_checked_completion(self):
        self.gate_computation()
        self.cli('run', 'a')
        environment = self.inject(job_states={'a__compute': 'UNKNOWN'})
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.wait_for(lambda: 'unresolved submission' in self.wait_messages())
        self.assertIsNone(process.poll(), self.wait_messages())
        (self.work / 'release').touch()
        self.assertEqual(process.wait(timeout=20), 0, self.wait_messages())
        self.assertEqual((self.work / 'wait.stdout').read_text(), '')

    def test_scheduler_failure_retries_quietly_then_reports_recovery(self):
        self.gate_computation()
        self.cli('run', 'a')
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() + "\nwith open('outage-imports', 'a') as stream: stream.write('loaded\\n')\n")
        environment = self.inject(fail_status_prefix='a__compute')
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.wait_for(lambda: 'Scheduler unavailable' in self.wait_messages() or process.poll() is not None)
        self.assertIsNone(process.poll(), self.wait_messages())
        self.wait_for(lambda: len((self.work / 'outage-imports').read_text().splitlines()) >= 3)
        self.assertEqual(self.wait_messages().count('Scheduler unavailable'), 1)
        self.inject()
        self.wait_for(lambda: 'Scheduler observations recovered' in self.wait_messages())
        (self.work / 'release').touch()
        self.assertEqual(process.wait(timeout=20), 0, self.wait_messages())
        self.assertEqual(self.wait_messages().count('Scheduler unavailable'), 1)

    def test_known_failure_wins_over_unknown_outcomes(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('echo a >>', 'exit 9; echo a >>'))
        self.cli('run', 'a')
        self.settle()
        for options in ({'job_states': {'a__gwflow_complete': 'UNKNOWN'}},
                        {'fail_status_prefix': 'a__gwflow_complete'}):
            with self.subTest(options=options):
                environment = self.inject(**options)
                result = self.cli_result('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment, success=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn('failed', result.stderr.lower())
                self.assertIn('may still be queued or running', result.stderr)

    def test_definition_change_ends_wait_even_with_unknown_job_outcome(self):
        self.gate_computation()
        self.cli('run', 'a')
        environment = self.inject(job_states={'a__compute': 'UNKNOWN'})
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.wait_for(lambda: 'unresolved submission' in self.wait_messages())
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('printf a >', 'printf changed >'))
        self.assertEqual(process.wait(timeout=8), 1, self.wait_messages())
        self.assertIn('changed commands', self.wait_messages())
        (self.work / 'release').touch()
        self.finish()

    def test_conflicting_job_ids_fail_instead_of_retrying_uncertainty(self):
        self.run_complete('a')
        environment = self.inject(conflicting_tracking_prefix='a__compute')
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.assertEqual(process.wait(timeout=8), 1, self.wait_messages())
        self.assertIn('Conflicting', self.wait_messages())

    def test_usage_rejects_invalid_intervals_and_unknown_selectors(self):
        for options in (('--interval', '0'), ('--interval', '-1'), ('--interval', 'nan'),
                        ('--interval', 'inf'), ('--interval', 'text'), ('missing',)):
            with self.subTest(options=options):
                result = self.cli_result('wait', *options, success=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')

    def test_loading_prints_and_child_output_stay_on_stderr(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nimport os, subprocess, sys\nprint('workflow stdout')\n"
                            "print('workflow stderr', file=sys.stderr)\n"
                            "os.write(1, b'descriptor stdout\\n')\n"
                            "subprocess.run(['printf', 'child stdout\\n'], check=True)\n")
        result = self.cli_result('wait', 'a', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        for message in ('workflow stdout', 'workflow stderr', 'descriptor stdout', 'child stdout'):
            self.assertIn(message, result.stderr)

    def test_no_attempt_requires_run_and_does_not_create_work(self):
        result = self.cli_result('wait', success=False)
        self.assertEqual(result.returncode, 1)
        for name in ('a', 'b', 'c'):
            self.assertIn(name + ': pending', result.stderr)
        self.assertFalse((self.work / 'work').exists())
        self.assertFalse((self.work / '.gwf/gwflow').exists())

    def test_required_upstream_failure_ends_wait_for_consumer(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('echo a >>', 'exit 9; echo a >>'))
        self.cli('run', 'c')
        self.settle()
        result = self.cli_result('wait', 'c', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('a:', result.stderr)
        self.assertIn('failed', result.stderr)
        self.assertNotIn('b:', result.stderr)

    def test_deferred_repair_requires_another_command_without_changing_work(self):
        self.run_complete()
        attempts = self.task_attempts()
        trace = (self.work / 'trace').read_bytes()
        (self.work / 'results/a/result.txt').unlink()
        result = self.cli_result('wait', 'c', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('deferred', result.stderr)
        self.assertEqual(self.task_attempts(), attempts)
        self.assertEqual((self.work / 'trace').read_bytes(), trace)
        self.assertFalse((self.work / 'results/a/result.txt').exists())

    def test_replaced_attempt_is_not_silently_followed(self):
        self.run_complete('a')
        environment = self.inject(queued_prefix='a__gwflow_complete')
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.wait_for(lambda: 'a: queued' in self.wait_messages())
        self.cli('run', '--force', 'a')
        self.assertEqual(process.wait(timeout=15), 1, self.wait_messages())
        self.assertIn('attempt replaced', self.wait_messages())
        self.finish()

    def test_changed_inputs_end_wait_despite_unknown_job_outcome(self):
        self.gate_computation()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('empty_task(inputs=[])', "empty_task(inputs=['input.txt'])", 1))
        self.cli('run', 'a')
        environment = self.inject(job_states={'a__compute': 'UNKNOWN'})
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.wait_for(lambda: 'unresolved submission' in self.wait_messages())
        (self.work / 'input.txt').write_text('changed input')
        self.assertEqual(process.wait(timeout=15), 1, self.wait_messages())
        self.assertIn('input metadata changed', self.wait_messages())
        (self.work / 'release').touch()
        self.settle()

    def test_removed_task_ends_wait_and_leaves_its_jobs_running(self):
        self.gate_computation()
        self.cli('run', 'a')
        process = self.start_wait('wait', '--interval', '0.1', 'a')
        self.wait_for(lambda: 'a: running' in self.wait_messages())
        (self.work / 'workflow.py').write_text('from gwflow import Workflow\ngwf = Workflow()\n')
        self.assertEqual(process.wait(timeout=15), 1, self.wait_messages())
        self.assertIn('Tasks removed', self.wait_messages())
        (self.work / 'release').touch()
        self.finish()

    def test_canceled_work_requires_retry_and_is_not_resubmitted(self):
        self.gate_computation()
        self.cli('run', 'a')
        self.cli('cancel', 'a')
        self.settle()
        attempts = self.task_attempts()
        result = self.cli_result('wait', 'a', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('canceled', result.stderr)
        self.assertEqual(self.task_attempts(), attempts)

    def test_default_pause_is_quiet_unlocks_inspection_and_interrupt_leaves_jobs_running(self):
        self.gate_computation()
        self.cli('run', 'a')
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() + "\nwith open('imports', 'a') as stream: stream.write('loaded\\n')\n")
        process = self.start_wait('wait', 'a')
        self.wait_for(lambda: 'a: running' in self.wait_messages())
        messages = self.wait_messages()
        time.sleep(0.35)
        self.assertEqual(self.wait_messages(), messages)
        self.assertEqual((self.work / 'imports').read_text(), 'loaded\n')
        with (self.work / '.gwf/gwflow-submission.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=10), 130, self.wait_messages())
        (self.work / 'release').touch()
        self.finish()
        self.assertEqual((self.work / 'trace').read_text(), 'a\n')

    def test_interrupt_cleans_detached_loading_descendants_and_temporary_resources(self):
        self.block_loading_with_detached_child()
        process = self.start_wait('wait', '--timeout', '60')
        self.wait_for((self.work / 'detached-pid').exists)
        pid = int((self.work / 'detached-pid').read_text())
        root = Path((self.work / 'inspection-root').read_text())
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=10), 130, self.wait_messages())
        self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertFalse(root.exists())
        for message in ('loading stdout', 'loading stderr', 'loading descriptor'):
            self.assertIn(message, self.wait_messages())
        self.assertEqual((self.work / 'wait.stdout').read_text(), '')

    def test_interrupt_during_frontend_lock_and_scheduler_query_cleans_inspection(self):
        self.gate_computation()
        self.cli('run', 'a')
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nimport os\nfrom pathlib import Path\nPath('inspection-pid').write_text(str(os.getpid()))\n")
        with (self.work / '.gwf/gwflow-submission.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = self.start_wait('wait', 'a')
            self.wait_for((self.work / 'inspection-pid').exists)
            process.send_signal(signal.SIGINT)
            self.assertEqual(process.wait(timeout=10), 130, self.wait_messages())
        environment = self.inject(hold_observation=True)
        process = self.start_wait('-b', 'recovery_fixture', 'wait', 'a', env=environment)
        self.wait_for((self.work / 'observation-held').exists)
        pid = int((self.work / 'inspection-pid').read_text())
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=10), 130, self.wait_messages())
        self.assertFalse(Path(f'/proc/{pid}').exists())
        (self.work / 'release').touch()
        self.finish()

    def completion_fault(self, **options):
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        (self.work / 'job-fault.json').write_text(json.dumps(options))
        return self.inject(job_fault='a__gwflow_complete')

    def test_wait_includes_required_record_compaction(self):
        environment = self.completion_fault(gate_before_compaction=True)
        self.cli('-b', 'recovery_fixture', 'run', 'a', env=environment)
        self.wait_for((self.work / 'a-compaction-held').exists)
        process = self.start_wait('wait', '--interval', '0.1', 'a')
        self.wait_for(lambda: 'a: finishing' in self.wait_messages())
        self.assertIsNone(process.poll())
        (self.work / 'a-compaction-release').touch()
        self.assertEqual(process.wait(timeout=20), 0, self.wait_messages())

    def test_failed_installation_requires_followup_without_repeating_computation(self):
        environment = self.completion_fault(crash_after_manifest=True)
        self.cli('-b', 'recovery_fixture', 'run', 'a', env=environment)
        self.settle()
        result = self.cli_result('wait', 'a', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('another command', result.stderr)
        self.assertEqual((self.work / 'trace').read_text(), 'a\n')
        self.assertFalse((self.work / 'results/a/result.txt').exists())

    def test_failed_compaction_does_not_accept_valid_retained_results(self):
        environment = self.completion_fault(compaction_fault='after-consolidating')
        self.cli('-b', 'recovery_fixture', 'run', 'a', env=environment)
        self.settle()
        self.assertEqual((self.work / 'results/a/result.txt').read_text(), 'a')
        result = self.cli_result('wait', 'a', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('compaction', result.stderr)
        self.assertEqual((self.work / 'trace').read_text(), 'a\n')

    def test_simulated_slurm_completion_uses_one_scheduler_snapshot(self):
        environment = test_managed_recovery.ManagedCoordinationTests.slurm_environment(self)
        environment['GWFLOW_TEST_RECORD_SLURM_QUERIES'] = '1'
        self.cli('-b', 'slurm', 'run', 'a', env=environment)
        self.finish()
        submitted = (self.work / 'slurm-submitted.jsonl').read_bytes()
        (self.work / 'slurm-queries.jsonl').unlink(missing_ok=True)
        result = self.cli_result('-b', 'slurm', 'wait', 'a', env=environment)
        self.assertEqual(result.stdout, '')
        self.assertEqual((self.work / 'slurm-submitted.jsonl').read_bytes(), submitted)
        queries = (self.work / 'slurm-queries.jsonl').read_text().splitlines()
        self.assertLessEqual(len(queries), 2)

    def test_other_loading_and_inspection_errors_fail_without_retries(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text()
        for failure in ("raise OSError('temporary fixture filesystem error')\n", "invalid syntax here\n"):
            with self.subTest(failure=failure):
                workflow.write_text(failure)
                result = self.cli_result('wait', '--interval', '0.1', success=False)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertIn('may still be queued or running', result.stderr)
                self.assertNotIn('Scheduler unavailable', result.stderr)
        workflow.write_text(declaration)
        self.run_complete('a')
        result = self.cli_result('-b', 'recovery_fixture', 'wait', 'a',
                                 env=self.inject(fail_tracking_prefix='a__compute'), success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('injected tracking lookup failure', result.stderr)
        self.assertNotIn('Scheduler unavailable', result.stderr)

    def test_unknown_outcome_does_not_hide_required_missing_submission(self):
        self.gate_computation()
        self.cli('-b', 'recovery_fixture', 'run', 'a', success=False,
                 env=self.inject(reject_prefix='a__compute'))
        environment = self.inject(job_states={'a__compute': 'UNKNOWN'})
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.assertEqual(process.wait(timeout=8), 1, self.wait_messages())
        self.assertIn('another command', self.wait_messages())
        (self.work / 'release').touch()
        self.finish()
        self.assertFalse((self.work / 'results/a/result.txt').exists())

    def test_active_recovered_submission_can_settle_without_another_command(self):
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        (self.work / 'job-fault.json').write_text(json.dumps({'crash_before_execution': True}))
        self.cli('-b', 'recovery_fixture', 'run', 'a', env=self.inject(job_fault='a__compute'))
        self.settle()
        document = json.loads(self.cli_result('status', '--json', '--details', 'a').stdout)
        old_submission = next(job['submission_name'] for job in document['tasks'][0]['jobs']
                              if job['name'] == 'a__compute')
        self.cli('-b', 'recovery_fixture', 'recover', 'a', env=self.inject(job_states={'a': 'UNKNOWN'}))
        self.cli('run', 'a')
        self.settle()
        environment = self.inject(submission_states={old_submission: 'RUNNING'})
        process = self.start_wait('-b', 'recovery_fixture', 'wait', '--interval', '0.1', 'a', env=environment)
        self.wait_for(lambda: 'recovered submissions' in self.wait_messages())
        self.assertIsNone(process.poll(), self.wait_messages())
        self.inject()
        self.assertEqual(process.wait(timeout=15), 0, self.wait_messages())
        self.assertEqual((self.work / 'trace').read_text(), 'a\n')
