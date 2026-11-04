"""Bounded local connections through the managed backend and installed CLI."""

import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from gwf.backends.exceptions import BackendError
from gwf.conf import FileConfig

from gwflow.backends import create_backend

from support import LocalBackendTestCase
import test_graphs
import test_wait


class ConnectionBudgetTests(unittest.TestCase):
    def test_absent_worker_exhausts_three_attempts_without_unbounded_backoff(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('socket.socket') as sockets, patch('time.sleep') as sleep:
            sockets.return_value.connect.side_effect = ConnectionRefusedError('worker absent')
            with self.assertRaises(BackendError) as failure:
                create_backend('local', directory, FileConfig.load(directory + '/.gwfconf.json'))
            self.assertEqual(sockets.return_value.connect.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.25])
            self.assertEqual(sockets.return_value.close.call_count, 3)
            self.assertEqual([call.args[0] for call in sockets.return_value.settimeout.call_args_list], [1, 1, 1])
            self.assertIn('gwflow workers', str(failure.exception))
            self.assertNotIn('gwf workers', str(failure.exception))

    def test_interrupt_during_connect_closes_socket_without_retrying(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('socket.socket') as sockets, patch('time.sleep') as sleep:
            sockets.return_value.connect.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                create_backend('local', directory, FileConfig.load(directory + '/.gwfconf.json'))
            sockets.return_value.close.assert_called_once()
            sleep.assert_not_called()


class ConnectionCliTests(LocalBackendTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    start_wait = test_wait.WaitTests.start_wait
    stop_wait = test_wait.WaitTests.stop_wait
    wait_messages = test_wait.WaitTests.wait_messages

    def test_absent_worker_preview_fails_promptly_and_releases_frontend_guard(self):
        self.stop_worker(self.worker)
        result = subprocess.run([self.executable, 'run', '--dry-run'], cwd=self.work,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('gwflow workers', result.stderr)
        self.assertNotIn('gwf workers', result.stderr)
        self.worker = self.start_worker()
        self.assertIn('Run', self.cli('run', '--dry-run'))
        self.assertFalse((self.work / 'trace').exists())

    def test_absent_workers_refuse_mutations_and_preserve_disk_inspection(self):
        self.run_complete()
        attempts = self.task_attempts()
        trace = (self.work / 'trace').read_text()
        workspace = self.work / 'work/sample' / attempts['sample']
        self.assertTrue(workspace.is_dir())
        self.stop_worker(self.worker)
        for arguments in (('run', '--force'), ('status', '--plain'), ('status', '--json'),
                          ('cancel', 'sample'), ('clean', '--delete')):
            with self.subTest(arguments=arguments):
                started = time.monotonic()
                output = self.cli(*arguments, success=False)
                self.assertLess(time.monotonic() - started, 5, output)
                self.assertIn('gwflow workers', output)
                self.assertNotIn('gwf workers', output)
                self.assertTrue(workspace.is_dir())
        self.assertIn('sample__left', self.cli('logs', 'sample'))
        self.cli('logs', 'sample__left', '--no-pager')
        self.assertIn('gwflow workers', self.cli('recover', 'sample'))
        self.assertEqual((self.work / 'results/sample/result.txt').read_text(), 'leftright')
        self.worker = self.start_worker()
        self.assertEqual(self.task_attempts(), attempts)
        self.assertEqual((self.work / 'trace').read_text(), trace)
        self.assertIn('Reuse', self.cli('run', '--dry-run'))

    def test_interrupt_during_connection_releases_guard_without_submitting(self):
        workflow = self.work / 'workflow.py'
        original = workflow.read_text()
        # Hold the external socket boundary, so interruption cannot miss a
        # short retry pause on a busy test host.
        workflow.write_text(original + "\nimport socket, time\nfrom pathlib import Path\n"
                            "def connect(sock, address):\n"
                            "    Path('connecting').touch()\n"
                            "    time.sleep(60)\n"
                            "socket.socket.connect = connect\n")
        process = self.start_wait('run', '--dry-run')
        self.wait_for(lambda: (self.work / 'connecting').exists() or process.poll() is not None)
        self.assertIsNone(process.poll(), self.wait_messages())
        process.send_signal(signal.SIGINT)
        self.assertNotEqual(process.wait(timeout=5), 0, self.wait_messages())
        workflow.write_text(original)
        self.assertIn('Run', self.cli('run', '--dry-run'))
        self.assertFalse((self.work / 'trace').exists())

    def test_wait_timeout_still_bounds_absent_worker_polling(self):
        self.run_complete()
        self.stop_worker(self.worker)
        started = time.monotonic()
        result = self.cli_result('wait', '--interval', '10', '--timeout', '5', success=False)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn('Scheduler unavailable', result.stderr)
        self.assertLess(time.monotonic() - started, 8, result.stderr)
        self.worker = self.start_worker()
        self.assertIn('Reuse', self.cli('run', '--dry-run'))

    def test_wait_polls_absent_workers_then_recovers_without_replacing_attempt(self):
        self.run_complete()
        attempts = self.task_attempts()
        self.stop_worker(self.worker)
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "\nwith open('inspections', 'a') as stream: stream.write('loaded\\n')\n")
        process = self.start_wait('wait', '--interval', '0.1', '--timeout', '15')
        self.wait_for(lambda: 'Scheduler unavailable' in self.wait_messages() or process.poll() is not None)
        self.assertIsNone(process.poll(), self.wait_messages())
        self.wait_for(lambda: len((self.work / 'inspections').read_text().splitlines()) >= 3)
        self.assertEqual(self.wait_messages().count('Scheduler unavailable'), 1)
        self.assertIn('gwflow workers', self.wait_messages())
        self.worker = self.start_worker()
        self.assertEqual(process.wait(timeout=15), 0, self.wait_messages())
        self.assertIn('Scheduler observations recovered', self.wait_messages())
        self.assertEqual(self.task_attempts(), attempts)

