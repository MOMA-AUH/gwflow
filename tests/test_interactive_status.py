"""Interactive status through the installed CLI and a controlling terminal."""

import fcntl
import os
from pathlib import Path
import shlex
import signal
import sys

from support import FIXTURES, GWFLOW, LocalBackendTestCase
from terminal_session import TerminalSession
import test_graphs
import test_grouped_status
import test_registry_images


class InteractiveStatusTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    inject = test_graphs.TaskGraphTests.inject
    slurm_environment = test_graphs.TaskGraphTests.slurm_environment

    def terminal(self, *args, **options):
        session = TerminalSession([GWFLOW, *args], cwd=self.work, **options)
        self.addCleanup(session.close)
        return session

    def test_default_terminal_opens_groups_with_colors_and_quits_cleanly(self):
        session = self.terminal('status', env={'NO_COLOR': '1'})
        session.wait_for('Groups')
        session.wait_for('sample')
        self.assertIsNone(session.process.poll())
        self.assertIn(b'\x1b[?1049h', session.output)
        self.assertRegex(session.output.decode(), r'\x1b\[(?:[0-9;]*;)?(?:3[0-7]|38);')
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertIn(b'\x1b[?1049l', session.output)
        self.assertGreater(session.output.rfind(b'\x1b[?25h'), session.output.rfind(b'\x1b[?25l'))

    def test_drill_down_reuses_captured_observations_and_releases_frontend_lock(self):
        self.run_complete()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nwith open('workflow-imports', 'a') as stream: stream.write('import\\n')\n")
        environment = self.inject(record_status=True)
        session = self.terminal('-b', 'recovery_fixture', 'status', env=environment)
        session.wait_for('sample')
        observed = (self.work / 'backend-observations.jsonl').read_bytes()
        self.assertTrue(observed)
        checkpoint = session.send('\r')
        session.wait_for('Instance', after=checkpoint, timeout=5)
        checkpoint = session.send('\r')
        session.wait_for('Jobs completed: 5/5', after=checkpoint, timeout=5)
        checkpoint = session.send('\r')
        session.wait_for('Public job:     sample__gwflow_prepare', after=checkpoint, timeout=5)
        session.wait_for('Backend job:', after=checkpoint)
        session.send('\x1b')
        session.send('?')
        session.wait_for('Keyboard reference')
        session.send('\x1b')
        session.send('g')
        self.assertEqual((self.work / 'workflow-imports').read_text(), 'import\n')
        self.assertEqual((self.work / 'backend-observations.jsonl').read_bytes(), observed)
        # A second frontend must complete while the browser remains open.
        self.cli('status', '--static')
        self.assertIsNone(session.process.poll())
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_initial_errors_keep_source_context_and_expandable_same_invocation_tracebacks(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text('from gwflow import Workflow\nif True\n    gwf = Workflow()\n')
        session = self.terminal('status')
        session.wait_for('No snapshot available')
        session.wait_for('workflow.py:2')
        session.send('q')
        self.assertEqual(session.finish(), 1, session.text)
        workflow.write_text(
            "with open('effects', 'a') as stream: stream.write('once\\n')\n"
            "print('captured workflow output')\n"
            "try:\n    raise KeyError('original failure')\n"
            "except KeyError as error:\n    raise ValueError('unexpected failure') from error\n")
        session = self.terminal('status')
        session.wait_for('No snapshot available')
        session.wait_for('unexpected failure')
        session.wait_for('Error details')
        checkpoint = session.send('\r')
        session.wait_for('Traceback', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 1, session.text)
        self.assertIn("KeyError: 'original failure'", session.text)
        self.assertIn('ValueError: unexpected failure', session.text)
        self.assertEqual((self.work / 'effects').read_text(), 'once\n')

    def test_initial_selection_filter_and_expanded_jobs_keep_group_denominators(self):
        test_grouped_status.GroupedStatusTests.configure_workflow(self)
        self.cli('run', 'mapping__A')
        self.finish()
        session = self.terminal('status', 'mapping*', '--status', 'pending', '--details')
        session.wait_for('Showing 2 of 3 selected Tasks', timeout=5)
        session.wait_for('mapping__C')
        session.wait_for('gwflow_prepare')
        self.assertNotIn('mapping__A', session.text)
        self.assertNotIn('remapping', session.text)
        checkpoint = session.send('g')
        session.wait_for('1/3', after=checkpoint)
        session.wait_for('2 Tasks match', after=checkpoint)
        self.assertNotIn('remapping', session.text)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_blocked_snapshot_keeps_workflow_notices_when_view_filter_is_empty(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace("left <<", "left.image = 'missing-image.sif'\nleft <<"))
        session = self.terminal('status', '--status', 'reusable')
        session.wait_for('Showing 0 of 1 selected Tasks')
        session.wait_for('Workflow blocked', timeout=5)
        session.send('\t')
        session.send('\x1b[F')
        session.wait_for('image unavailable', timeout=5)
        self.assertNotIn('No snapshot available', session.text)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_fresh_expanded_jobs_have_known_pending_state_without_submissions(self):
        session = self.terminal('status', '--details')
        session.wait_for('gwflow_prepare')
        self.assertNotIn('unknown', session.text)
        session.send('\x1b[B\r')
        session.wait_for('Observed state:')
        self.assertRegex(session.text, r'Observed state:\s*○ pending')
        session.wait_for('not submitted')
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_inspection_owns_detached_orphaned_descendants_on_quit_and_completion(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text()
        child = ("from pathlib import Path; import os, signal, time\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                 "Path('detached-pid').write_text(str(os.getpid()))\n"
                 "while not Path('detached-release').exists(): time.sleep(0.025)\n"
                 "Path('detached-late-effect').touch()\n")
        intermediate = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)"
        for stalled in (True, False):
            with self.subTest(stalled=stalled):
                workflow.write_text("import subprocess, sys, time\n"
                                    f"subprocess.run([sys.executable, '-c', {intermediate!r}], start_new_session=True)\n" +
                                    ("time.sleep(60)\n" if stalled else "") + declaration)
                session = self.terminal('status')
                pidfile = self.work / 'detached-pid'
                self.wait_for(pidfile.exists)
                pid = int(pidfile.read_text())
                try:
                    session.wait_for('Inspecting workflow' if stalled else 'sample')
                    session.send('q')
                    self.assertEqual(session.finish(), 1 if stalled else 0, session.text)
                    self.assertFalse(Path(f'/proc/{pid}').exists(), f'Owned detached descendant {pid} survived')
                    self.assertFalse((self.work / 'detached-late-effect').exists())
                finally:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                pidfile.unlink()

    def test_quit_stops_import_descendants_and_lock_and_scheduler_waits(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text()
        child = ("from pathlib import Path; import os, time\n"
                 "Path('child-pid').write_text(str(os.getpid()))\n"
                 "while not Path('child-release').exists(): time.sleep(0.025)\n"
                 "Path('late-effect').touch()\n")
        workflow.write_text("import subprocess, sys, time\n"
                            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                            "time.sleep(60)\n" + declaration)
        session = self.terminal('status')
        self.wait_for((self.work / 'child-pid').exists)
        child_pid = int((self.work / 'child-pid').read_text())
        session.wait_for('Inspecting workflow')
        session.send('q')
        self.assertEqual(session.finish(), 1, session.text)
        def child_running():
            try:
                return Path(f'/proc/{child_pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
            except FileNotFoundError:
                return False
        self.wait_for(lambda: not child_running())
        (self.work / 'child-release').touch()
        self.assertFalse((self.work / 'late-effect').exists())
        workflow.write_text(declaration)
        lock_path = self.work / '.gwf/gwflow-submission.lock'
        lock_path.parent.mkdir(exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for key in ('q', '\x03'):
                session = self.terminal('status')
                session.wait_for('Waiting for frontend')
                session.send(key)
                self.assertEqual(session.finish(), 1, session.text)
                if key == '\x03':
                    self.assertIn('Aborted!', session.text)
        session = self.terminal('-b', 'recovery_fixture', 'status', env=self.inject(stall_connection=True))
        session.wait_for('Waiting for scheduler')
        session.send('q')
        self.assertEqual(session.finish(), 1, session.text)
        self.cli('status', '--static')

    def test_quit_leaves_submitted_work_running(self):
        held, release = self.work / 'running-held', self.work / 'running-release'
        self.configure_workflow(left_command=(
            f'touch {shlex.quote(str(held))}; '
            f'while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; printf left > same.txt'))
        self.cli('run')
        self.wait_for(held.exists)
        try:
            session = self.terminal('status')
            session.wait_for('sample')
            session.send('q')
            self.assertEqual(session.finish(), 0, session.text)
        finally:
            release.touch()
        self.finish()
        self.assertEqual((self.work / 'results/sample/result.txt').read_text(), 'leftright')

    def test_static_modes_and_unavailable_or_unsupported_terminals_never_open_browser(self):
        cases = [
            (('--static',), {}, False),
            (('--plain',), {}, True),
            (('--static', '--plain'), {}, True),
            ((), {'input_mode': 'unavailable'}, False),
            ((), {'input_mode': 'write-only'}, False),
            ((), {'term': 'dumb'}, True),
            ((), {'encoding': 'ascii'}, True),
            ((), {'size': (39, 12)}, True),
        ]
        for options, terminal, plain in cases:
            with self.subTest(options=options, terminal=terminal):
                session = self.terminal('status', '*', *options, **terminal)
                self.assertEqual(session.finish(), 0, session.text)
                self.assertNotIn(b'\x1b[?1049h', session.output)
                self.assertIn('0/5', session.text)
                if plain:
                    self.assertNotIn(b'\x1b[', session.output)
                else:
                    self.assertIn('›', session.text)
                if terminal.get('size'):
                    self.assertIn('Terminal too small', session.text)

    def test_workflow_output_and_diagnostics_are_captured_and_available_from_help(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nprint('captured workflow output')\nimport logging\n"
                            "logging.warning('captured workflow warning')\n"
                            "logging.debug('captured debug detail')\n")
        session = self.terminal('--verbose', 'debug', 'status')
        session.wait_for('sample')
        self.assertNotIn('captured workflow output', session.text)
        self.assertNotIn('captured workflow warning', session.text)
        session.send('?')
        session.wait_for('Keyboard reference')
        session.send('\t\r')
        session.wait_for('captured workflow output', timeout=5)
        session.wait_for('captured workflow warning')
        session.wait_for('captured debug detail')
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_unexpected_interface_failure_restores_terminal_and_stops_inspection(self):
        session = TerminalSession(
            [sys.executable, str(FIXTURES / 'browser_fault.py'), '-b', 'recovery_fixture', 'status'],
            cwd=self.work, env=self.inject(stall_connection=True))
        self.addCleanup(session.close)
        session.wait_for('Waiting for scheduler')
        session.send('x')
        self.assertEqual(session.finish(), 1, session.text)
        self.assertIn("KeyError: 'original interface failure'", session.text)
        self.assertIn('RuntimeError: injected interface failure', session.text)
        self.assertGreater(session.output.rfind(b'Interactive status failed unexpectedly'),
                           session.output.rfind(b'\x1b[?1049l'))
        self.assertIn(b'\x1b[?1049l', session.output)
        self.cli('status', '--static')

    def test_simulated_slurm_browsing_reuses_scheduler_observations(self):
        environment = {**self.slurm_environment(), 'GWFLOW_TEST_RECORD_SLURM_QUERIES': '1'}
        self.cli('-b', 'slurm', 'run', env=environment)
        self.finish()
        session = self.terminal('-b', 'slurm', 'status', env=environment)
        session.wait_for('sample')
        queries = (self.work / 'slurm-queries.jsonl').read_bytes()
        self.assertIn(b'squeue', queries)
        self.assertIn(b'sacct', queries)
        session.send('\r')
        session.wait_for('Instance')
        session.send('\r')
        session.wait_for('Jobs completed: 5/5')
        session.send('\r')
        session.wait_for('Public job:     sample__gwflow_prepare')
        session.wait_for('Backend job:')
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertEqual((self.work / 'slurm-queries.jsonl').read_bytes(), queries)

    def test_real_terminal_resize_keeps_the_view_and_inspection(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nwith open('resize-imports', 'a') as stream: stream.write('import\\n')\n")
        session = self.terminal('status', '*', size=(40, 12))
        session.wait_for('0/5')
        session.resize(30, 8)
        session.wait_for('Terminal too small')
        checkpoint = len(session.output)
        session.resize(80, 24)
        session.wait_for('0/5', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertEqual((self.work / 'resize-imports').read_text(), 'import\n')

    def test_job_details_retain_observed_submission_state_when_work_must_repeat(self):
        self.run_complete()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('printf right > same.txt', 'printf changed > same.txt'))
        session = self.terminal('status', 'sample')
        session.wait_for('0/5')
        session.send('\r')
        session.wait_for('Jobs completed: 0/5')
        session.send('\r')
        session.wait_for('Public job:     sample__gwflow_prepare')
        session.wait_for('Observed state:')
        self.assertRegex(session.text, r'Observed state:\s*✓ completed')
        session.wait_for('Requires repetition', timeout=5)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)


class InteractiveImageInspectionTests(test_registry_images.RegistryTestCase):
    terminal = InteractiveStatusTests.terminal

    def test_quitting_acquisition_stops_pull_and_cleans_owned_temporary_files(self):
        self.options(gate='browser')
        session = self.terminal('status')
        self.wait_for((self.controller / 'browser-held').exists)
        session.wait_for('Pulling image:')
        session.send('q')
        self.assertEqual(session.finish(), 1, session.text)
        self.assertEqual(list(self.cache.glob('.pull-*')), [])
        self.assertEqual(self.calls('pull-success'), [])
        (self.controller / 'browser-release').touch()
        self.options()
        self.cli('status', '--static')
        self.assertEqual(len(self.calls('pull')), 2)
        self.assertEqual(len(self.cached_images()), 1)
