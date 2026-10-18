"""Common command presentation through installed CLI processes."""

import os
import re
import fcntl
import subprocess
import threading

from support import GWFLOW, LocalBackendTestCase
import test_graphs
import test_fresh


class CommandPresentationTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    inject = test_fresh.FreshAttemptTests.inject

    def test_color_environment_replaces_flags_and_config_without_rewriting_it(self):
        self.configure(no_color=True)
        before = (self.work / '.gwfconf.json').read_bytes()
        colored = self.terminal_cli('status', '*', env={'NO_COLOR': ''}).stdout
        self.assertIn('\x1b[', colored)
        monochrome = self.terminal_cli('status', '*', env={'NO_COLOR': '1'}).stdout
        self.assertNotIn('\x1b[', monochrome)
        self.assertRegex(monochrome, r'0/5 \[─+\]')
        for flag in ('--no-color', '--use-color'):
            self.assertNotIn(flag, self.cli('--help'))
            result = self.cli_result(flag, 'status', success=False)
            self.assertEqual(result.returncode, 2)
        for args in (('status', '*'), ('status', '*', '--plain'), ('run', '--dry-run')):
            result = self.cli_result('--verbose', 'debug', *args, env={**os.environ, 'NO_COLOR': ''})
            self.assertNotIn('\x1b[', result.stdout + result.stderr)
        self.assertEqual((self.work / '.gwfconf.json').read_bytes(), before)

    def test_syntax_errors_are_concise_and_unexpected_chains_are_captured_once(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text('from gwflow import Workflow\nif True\n    gwf = Workflow()\n')
        result = self.cli_result('status', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('could not load the workflow', result.stderr)
        self.assertIn('workflow.py:2', result.stderr)
        self.assertIn("expected ':'", result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        workflow.write_text(
            "from pathlib import Path\n"
            "with Path('effects.txt').open('a') as stream: stream.write('once\\n')\n"
            "try:\n    raise KeyError('original failure')\n"
            "except KeyError as error:\n    raise ValueError('unexpected failure') from error\n")
        result = self.cli_result('run', success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('run failed unexpectedly', result.stderr)
        self.assertIn('Traceback:', result.stderr)
        self.assertIn("KeyError: 'original failure'", result.stderr)
        self.assertIn('ValueError: unexpected failure', result.stderr)
        self.assertIn('direct cause', result.stderr)
        self.assertEqual((self.work / 'effects.txt').read_text(), 'once\n')

    def test_inspection_activity_uses_stderr_and_terminal_elapsed_time(self):
        result = self.cli_result('status', '*')
        self.assertIn('Inspecting workflow', result.stderr)
        self.assertIn('Inspecting Tasks', result.stderr)
        self.assertIn('Waiting for scheduler', result.stderr)
        self.assertNotIn('Inspecting', result.stdout)
        self.assertNotRegex(result.stderr, r'\x1b\[|\d+:\d{2}:\d{2}|%')
        terminal = self.terminal_cli('status', '*', merge_stderr=True).stdout
        self.assertIn('Inspecting workflow', terminal)
        self.assertRegex(terminal, r'\d+:\d{2}:\d{2}')
        self.assertNotIn('ETA', terminal)
        plain = self.terminal_cli('status', '*', '--plain', merge_stderr=True).stdout
        self.assertIn('Inspecting workflow', plain)
        self.assertNotRegex(plain, r'\x1b\[|\d+:\d{2}:\d{2}')

    def test_caught_cancellation_failure_retains_receipts_and_traceback(self):
        self.run_complete()
        environment = self.inject(job_states={'sample__left': 'RUNNING', 'sample__right': 'RUNNING'},
                                  reject_cancel_prefix='sample__left', accept_cancel_only=True,
                                  record_cancellations=True)
        result = self.cli_result('-b', 'recovery_fixture', 'cancel', 'sample', env=environment, success=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Cancellation requested', result.stdout)
        self.assertIn('Cancellation request failed', result.stdout)
        self.assertIn('Traceback:', result.stderr)
        self.assertIn('RuntimeError: injected cancellation rejection', result.stderr)
        self.assertEqual(len((self.work / 'cancellation-requests.jsonl').read_text().splitlines()), 2)

    def test_submission_wait_remains_visible_after_planning_finishes(self):
        environment = self.inject(hold_submission_prefix='sample__gwflow_prepare')
        progress = self.work / 'submission-progress.log'
        with progress.open('w') as stream, subprocess.Popen(
            [GWFLOW, '-b', 'recovery_fixture', 'run'], cwd=self.work, env=environment,
            stdout=subprocess.PIPE, stderr=stream, text=True,
        ) as process:
            try:
                self.wait_for((self.work / 'submission-held').exists)
                self.assertIsNone(process.poll())
                phases = progress.read_text()
                self.assertEqual(phases.count('Waiting for scheduler...'), 2, phases)
                self.assertTrue(phases.endswith('Waiting for scheduler...\n'), phases)
                self.assertNotIn('\x1b[', phases)
            finally:
                (self.work / 'submission-release').touch()
            output, _ = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, output + progress.read_text())

    def test_frontend_lock_wait_reports_elapsed_time_and_restores_cursor(self):
        path = self.work / '.gwf/gwflow-submission.lock'
        path.parent.mkdir(exist_ok=True)
        for terminal in (False, True):
            with path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                release = threading.Timer(1, fcntl.flock, args=(lock, fcntl.LOCK_UN))
                release.start()
                try:
                    result = (self.terminal_cli('status', '*', merge_stderr=True) if terminal
                              else self.cli_result('status', '*'))
                finally:
                    release.cancel()
                    release.join()
            output = result.stdout if terminal else result.stderr
            self.assertIn('Waiting for frontend submission bookkeeping', output)
            if terminal:
                self.assertRegex(output, r'\d+:\d{2}:\d{2}')
                self.assertGreater(output.rfind('\x1b[?25h'), output.rfind('\x1b[?25l'))
            else:
                self.assertEqual(output.count('Waiting for frontend'), 1)
                self.assertNotIn('\x1b[', output)
