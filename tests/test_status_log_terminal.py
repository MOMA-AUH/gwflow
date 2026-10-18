"""Submission log/history keys and read cancellation in an installed real TTY."""

import os
from pathlib import Path
import subprocess
import tempfile

from support import FIXTURES, LocalBackendTestCase
import test_graphs
import test_interactive_status


class StatusLogTerminalTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    inject = test_graphs.TaskGraphTests.inject
    terminal = test_interactive_status.InteractiveStatusTests.terminal

    def test_stream_history_and_paging_keys_do_not_reinspect_the_workflow(self):
        self.configure_workflow(left_command=("for i in $(seq 1 500); do printf 'TTYLINE %04d\\n' \"$i\"; done; "
                                               'echo TTYOUT; echo TTYERR >&2; printf left > same.txt'))
        self.run_complete()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nwith open('tty-log-imports', 'a') as stream: stream.write('import\\n')\n")
        session = self.terminal('-b', 'recovery_fixture', 'status', 'sample', '--details',
                                env=self.inject(record_status=True), size=(120, 40))
        session.wait_for('5/5')
        observations = (self.work / 'backend-observations.jsonl').read_bytes()
        checkpoint = session.send('\x1b[B' * 3 + 'l')
        session.wait_for('Choose log stream', after=checkpoint)
        checkpoint = session.send('\r')
        session.wait_for('TTYOUT', after=checkpoint)
        path = (self.work / '.gwf/logs/sample__left.stdout').resolve()
        with path.open('a') as stream:
            stream.write('APPENDED_BY_TEST\n')
        checkpoint = session.send('u')
        session.wait_for('APPENDED_BY_TEST', after=checkpoint)
        checkpoint = session.send('\x1b[H')
        session.wait_for('bytes 0–', after=checkpoint)
        checkpoint = session.send('\x1b[F')
        session.wait_for('APPENDED_BY_TEST', after=checkpoint)
        checkpoint = session.send('\x1b')
        session.wait_for('Selected Tasks', after=checkpoint)
        checkpoint = session.send('l')
        session.wait_for('Choose log stream', after=checkpoint)
        checkpoint = session.send('\x1b[B\r')
        session.wait_for('TTYERR', after=checkpoint)
        checkpoint = session.send('\x1b')
        session.wait_for('Selected Tasks', after=checkpoint)
        checkpoint = session.send('h')
        session.wait_for('History read at', after=checkpoint)
        session.wait_for('Recorded evidence', after=checkpoint)
        self.assertEqual((self.work / 'tty-log-imports').read_text(), 'import\n')
        self.assertEqual((self.work / 'backend-observations.jsonl').read_bytes(), observations)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertIn(b'\x1b[?1049l', session.output)

    def test_status_refresh_keeps_retired_log_then_returns_from_removed_task_to_groups(self):
        self.configure_workflow(left_command='echo ORIGINAL_STREAM; printf left > same.txt')
        self.run_complete()
        session = self.terminal('status', size=(120, 40))
        session.wait_for('sample')
        for expected in ('Tasks in sample', 'Jobs completed:'):
            checkpoint = session.send('\r')
            session.wait_for(expected, after=checkpoint)
        checkpoint = session.send('\x1b[B' * 2 + 'l')
        session.wait_for('Choose log stream', after=checkpoint)
        checkpoint = session.send('\r')
        session.wait_for('ORIGINAL_STREAM', after=checkpoint)
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('ORIGINAL_STREAM', 'NEW_STREAM'))
        self.cli('run', '--force')
        self.finish()
        checkpoint = session.send('r')
        session.wait_for('Current submission changed', after=checkpoint)
        self.assertNotIn('NEW_STREAM', session.output[checkpoint:].decode(errors='replace'))
        workflow.write_text('from gwflow import Workflow\ngwf = Workflow()\n')
        checkpoint = session.send('r')
        session.wait_for('Task no longer in the refreshed selection', after=checkpoint)
        checkpoint = session.send('\x1b')
        session.wait_for('Showing 0 of 0 selected Tasks', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_quit_reaps_a_log_reader_blocked_in_a_real_file_read(self):
        self.configure_workflow(left_command='echo BLOCKED_FILE_CONTENT; printf left > same.txt')
        self.run_complete()
        preload = tempfile.TemporaryDirectory(prefix='gwflow-log-preload-')
        self.addCleanup(preload.cleanup)
        library = Path(preload.name) / 'probe.so'
        compiled = subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-o', str(library),
                                   str(FIXTURES / 'log_read_probe.c'), '-ldl'], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        gate, started = self.work / 'read-gate', self.work / 'read-started'
        gate.touch()
        path = (self.work / '.gwf/logs/sample__left.stdout').resolve()
        session = self.terminal('status', 'sample', '--details', size=(120, 40), env={
            'LD_PRELOAD': str(library) + ' ' + os.environ.get('LD_PRELOAD', ''),
            'GWFLOW_LOG_READ_PATH': str(path), 'GWFLOW_LOG_READ_GATE': str(gate),
            'GWFLOW_LOG_READ_STARTED': str(started),
        })
        session.wait_for('5/5')
        checkpoint = session.send('\x1b[B' * 3 + 'l')
        session.wait_for('Choose log stream', after=checkpoint)
        checkpoint = session.send('\r')
        session.wait_for('Reading log...', after=checkpoint)
        self.wait_for(started.exists)
        pid = int(started.read_text())
        self.assertTrue(Path(f'/proc/{pid}').exists())
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertIn(b'\x1b[?1049l', session.output)
