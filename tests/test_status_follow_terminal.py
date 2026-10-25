"""Live following through an installed CLI with a real controlling terminal."""

import os
from pathlib import Path
import subprocess
import tempfile
import time

from support import LocalBackendTestCase
import test_graphs
import test_interactive_status


class StatusFollowTerminalTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    inject = test_graphs.TaskGraphTests.inject
    terminal = test_interactive_status.InteractiveStatusTests.terminal

    def test_follow_keys_mouse_pause_and_file_recovery_do_not_reinspect(self):
        self.configure_workflow(left_command=("for i in $(seq 1 500); do printf 'FOLLOWLINE %04d\\n' \"$i\"; done; "
                                               'printf left > same.txt'))
        self.run_complete()
        preload = tempfile.TemporaryDirectory(prefix='gwflow-follow-preload-')
        self.addCleanup(preload.cleanup)
        library = Path(preload.name) / 'probe.so'
        compiled = subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-o', str(library),
                                   str(Path(__file__).parent / 'fixtures/log_read_probe.c'), '-ldl'],
                                  capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        path = (self.work / '.gwf/logs/sample__left.stdout').resolve()
        gate, started = self.work / 'read-gate', self.work / 'read-started'
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nwith open('follow-imports', 'a') as stream: stream.write('import\\n')\n")
        session = self.terminal('-b', 'recovery_fixture', 'status', 'sample', '--details',
                                env={**self.inject(record_status=True),
                                     'LD_PRELOAD': str(library) + ' ' + os.environ.get('LD_PRELOAD', ''),
                                     'GWFLOW_LOG_READ_PATH': str(path), 'GWFLOW_LOG_READ_GATE': str(gate),
                                     'GWFLOW_LOG_READ_STARTED': str(started)}, size=(100, 30))
        session.wait_for('5/5')
        observed = (self.work / 'backend-observations.jsonl').read_bytes()
        checkpoint = session.send('\x1b[B' * 2 + 'l')
        session.wait_for('Choose log stream', after=checkpoint)
        checkpoint = session.send('\r')
        session.wait_for('FOLLOWLINE 0500', after=checkpoint)
        session.wait_for('Read at:', after=checkpoint)
        with path.open('a') as stream:
            stream.write('TTY_FOLLOW_OFF\n')
        deadline = time.monotonic() + 1.3
        while time.monotonic() < deadline:
            session.read()
        self.assertNotIn('TTY_FOLLOW_OFF', session.text)
        checkpoint = session.send('F')
        session.wait_for('(live)', after=checkpoint)
        session.wait_for('TTY_FOLLOW_OFF', after=checkpoint)
        with path.open('a') as stream:
            stream.write('TTY_FOLLOW_APPEND\n')
        session.wait_for('TTY_FOLLOW_APPEND', after=checkpoint, timeout=4)
        checkpoint = session.send('\x1b[<64;20;15M')
        session.wait_for('Read at:', after=checkpoint, timeout=3)
        checkpoint = session.send('\x1b[H')
        session.wait_for('Showing first 200 lines.', after=checkpoint)
        checkpoint = session.send('\x1b[F')
        session.wait_for('TTY_FOLLOW_APPEND', after=checkpoint)
        checkpoint = session.send('F')
        session.wait_for('(live)', after=checkpoint)
        replacement = path.with_suffix('.replacement')
        replacement.write_text('TTY_REPLACED\n' * 40)
        replacement.replace(path)
        session.wait_for('Log replaced', after=checkpoint, timeout=4)
        session.wait_for('TTY_REPLACED', after=checkpoint)
        checkpoint = len(session.output)
        path.unlink()
        session.wait_for('No stdout log available.', after=checkpoint, timeout=4)
        checkpoint = len(session.output)
        path.write_text('TTY_READABLE_AGAIN\n')
        session.wait_for('Log readable again', after=checkpoint, timeout=4)
        session.wait_for('TTY_READABLE_AGAIN', after=checkpoint)
        self.assertEqual((self.work / 'follow-imports').read_text(), 'import\n')
        self.assertEqual((self.work / 'backend-observations.jsonl').read_bytes(), observed)
        checkpoint = session.send('F')
        session.wait_for('Read at:', after=checkpoint)
        started.unlink(missing_ok=True)
        gate.touch()
        checkpoint = session.send('F')
        session.wait_for('(live)', after=checkpoint)
        self.wait_for(started.exists)
        reader_pid = int(started.read_text())
        self.assertTrue(Path(f'/proc/{reader_pid}').exists())
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertFalse(Path(f'/proc/{reader_pid}').exists())
        self.assertIn(b'\x1b[?1049l', session.output)

    def test_refresh_keeps_following_a_retired_submission_then_a_removed_task(self):
        self.configure_workflow(left_command='echo ORIGINAL_FOLLOW_STREAM; printf left > same.txt')
        self.run_complete()
        path = (self.work / '.gwf/logs/sample__left.stdout').resolve()
        session = self.terminal('status', size=(100, 30))
        session.wait_for('sample')
        for expected in ('Instance', 'Jobs completed:'):
            checkpoint = session.send('\r')
            session.wait_for(expected, after=checkpoint)
        checkpoint = session.send('\x1b[Bl')
        session.wait_for('Choose log stream', after=checkpoint)
        checkpoint = session.send('\r')
        session.wait_for('ORIGINAL_FOLLOW_STREAM', after=checkpoint)
        checkpoint = session.send('F')
        session.wait_for('(live)', after=checkpoint)
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('ORIGINAL_FOLLOW_STREAM', 'NEW_SUBMISSION_STREAM'))
        self.cli('run', '--force')
        self.finish()
        checkpoint = session.send('r')
        session.wait_for('Current submission changed', after=checkpoint)
        with path.open('a') as stream:
            stream.write('RETIRED_FOLLOW_APPEND\n')
        session.wait_for('RETIRED_FOLLOW_APPEND', after=checkpoint, timeout=4)
        self.assertNotIn('NEW_SUBMISSION_STREAM', session.output[checkpoint:].decode(errors='replace'))
        workflow.write_text('from gwflow import Workflow\ngwf = Workflow()\n')
        checkpoint = session.send('r')
        session.wait_for('Task no longer in the refreshed selection', after=checkpoint)
        with path.open('a') as stream:
            stream.write('REMOVED_TASK_FOLLOW_APPEND\n')
        session.wait_for('REMOVED_TASK_FOLLOW_APPEND', after=checkpoint, timeout=4)
        checkpoint = session.send('\x1b')
        session.wait_for('Showing 0 of 0 selected Tasks', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertIn(b'\x1b[?1049l', session.output)
