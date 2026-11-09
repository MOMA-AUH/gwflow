"""Native per-workflow scheduling through installed managed commands."""

import json
from pathlib import Path
import shlex
import subprocess

from gwf.backends.local import Client, get_socket_path

from support import GWF, GWFLOW, LocalBackendTestCase, TASK_FACTORY
from process_probe import ProcessProbe


class NativeLocalTests(LocalBackendTestCase):
    def configure_workflow(self, **options):
        (self.work / 'workflow.py').write_text(
            TASK_FACTORY + "from gwflow import Workflow, shell\n"
            "gwf = Workflow()\n"
            "task = empty_task(inputs=[])\n"
            "first = task.target('first', inputs=[], outputs=['intermediate'])\n"
            "first << 'printf hello > intermediate; echo native-stdout; echo native-stderr >&2'\n"
            "second = task.target('second', inputs=[first.output('intermediate')], outputs=['out'])\n"
            "second << shell('cat {source} > out', source=first.output('intermediate'))\n"
            "task.retain('result', source=second.output('out'), path='result.txt')\n"
            "gwf.task(task, alias='sample')\n")

    def test_native_task_completes_compacts_cleans_and_reuses(self):
        self.run_complete()
        self.assertEqual((self.work / 'results/sample/result.txt').read_text(), 'hello')
        self.assertIn('native-stdout', self.cli('logs', 'sample__first', '--no-pager'))
        self.assertIn('native-stderr', self.cli('logs', 'sample__first', '--stderr', '--no-pager'))
        self.assertIn('reusable', self.cli('status', '--plain'))
        self.cli('wait', '--timeout', '5')
        records = list((self.work / '.gwf/gwflow').glob('owners/*/tasks/sample/attempts/*/record-state.json'))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())['phase'], 'compacted')
        before = json.loads((self.work / '.gwf/local-backend-tracked.json').read_text())
        self.assertEqual(len(before), 4)
        self.assertTrue(all(isinstance(value, str) and '.' in value for value in before.values()))
        self.cli('clean', '--delete')
        self.assertIn('Reuse', self.cli('run', '--dry-run'))
        self.assertNoSubmissions(self.cli('run'))
        self.assertEqual(json.loads((self.work / '.gwf/local-backend-tracked.json').read_text()), before)

    def test_workflow_directories_are_independent_and_nested_commands_find_root(self):
        other = self.make_workdir()
        (other / 'workflow.py').write_text((self.work / 'workflow.py').read_text())
        (other / '.gwfconf.json').write_text(json.dumps(self.config))
        self.start_worker(working_dir=other)
        nested = self.work / 'nested'
        nested.mkdir()
        result = subprocess.run([GWFLOW, 'run'], cwd=nested, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.finish()
        self.assertEqual((self.work / 'results/sample/result.txt').read_text(), 'hello')
        with Client.connect(get_socket_path(other)) as client:
            self.assertEqual(client.status(), {})
        self.assertFalse((other / 'results').exists())
        self.assertFalse((nested / '.gwf').exists())
        duplicate = subprocess.run([GWFLOW, 'workers'], cwd=nested, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn('already running', duplicate.stderr)
        self.stop_worker(self.worker)
        missing = self.cli('status', '--plain', success=False)
        self.assertIn('gwflow workers', missing)
        self.assertIn(str(self.work), missing)

    def test_ordinary_empty_worker_cannot_accept_managed_work(self):
        self.stop_worker(self.worker)
        self.executable = GWF
        self.worker = self.start_worker()
        self.executable = GWFLOW
        self.assertIn('gwflow workers', self.cli('run', success=False))
        with Client.connect(get_socket_path(self.work)) as client:
            self.assertEqual(client.status(), {})
        self.assertFalse((self.work / 'results/sample/result.txt').exists())

    def test_retired_host_and_port_are_usage_errors(self):
        for option in ('--host', '--port'):
            with self.subTest(option=option):
                result = self.cli_result('workers', option, '12345', success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn('No such option', result.stderr)
        self.configure(**{'backend.local.port': 12345})
        self.assertIn('remove retired', self.cli('run', success=False))

    def test_both_streams_are_readable_before_gated_computation_finishes(self):
        gate = self.work / 'release'
        self.addCleanup(gate.touch)
        workflow = self.work / 'workflow.py'
        source = workflow.read_text()
        command = ('printf hello > intermediate; echo native-stdout; echo native-stderr >&2; '
                   f'while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.025; done')
        workflow.write_text(source.replace(
            "'printf hello > intermediate; echo native-stdout; echo native-stderr >&2'", repr(command)))
        self.cli('run')
        # gwf.exec flushes small output periodically; the bounded wait allows it.
        def visible():
            result = subprocess.run([GWFLOW, 'logs', 'sample__first', '--no-pager'],
                                    cwd=self.work, capture_output=True, text=True, timeout=10)
            return result.returncode == 0 and 'native-stdout' in result.stdout
        self.wait_for(visible)
        self.assertIn('native-stderr', self.cli('logs', 'sample__first', '--stderr', '--no-pager'))
        self.assertIn('1 active', self.cli('status', '--plain'))
        self.assertFalse((self.work / 'results/sample/result.txt').exists())
        gate.touch()
        self.finish()


class NativeTerminationTests(ProcessProbe, LocalBackendTestCase):
    def configure_workflow(self, **options):
        import test_graphs
        test_graphs.TaskGraphTests.configure_workflow(self, **options)

    def start_probe(self, *, walltime=None):
        self.configure_workflow(right_command=self.probe(escape=True))
        if walltime is not None:
            workflow = self.work / 'workflow.py'
            workflow.write_text(workflow.read_text().replace(
                "right = task.target('right', inputs=[], outputs=['same.txt'])",
                f"right = task.target('right', inputs=[], outputs=['same.txt'], walltime={walltime!r})"))
        self.cli('run')
        self.wait_for((self.work / 'held').exists)

    def test_cancellation_stops_descendants_before_retry_and_cleanup(self):
        self.start_probe()
        self.assertIn('may still be stopping', self.cli('cancel', 'sample'))
        self.settle()
        self.assertEqual(self.running_probe_processes(), [])
        self.assertIn('Retry', self.cli('run', '--dry-run'))
        self.cli('clean', 'sample', '--delete')
        (self.work / 'release').touch()
        self.assertFalse((self.work / 'late-write').exists())

    def test_short_walltime_stops_descendants_before_retry(self):
        self.start_probe(walltime='00:00:02')
        self.settle()
        self.assertEqual(self.running_probe_processes(), [])
        self.assertIn('Retry', self.cli('run', '--dry-run'))
        self.assertFalse((self.work / 'results/sample/result.txt').exists())

    def test_shutdown_stops_owned_descendants(self):
        self.start_probe()
        self.stop_worker(self.worker)
        self.assertEqual(self.running_probe_processes(), [])

    def test_worker_channel_loss_stops_descendants_but_stays_unresolved(self):
        self.start_probe()
        self.worker.kill()
        self.worker.communicate(timeout=10)
        self.wait_for(lambda: not self.running_probe_processes())
        self.worker = self.start_worker()
        self.assertIn('cannot confirm computation descendants have stopped',
                      self.cli('run', '--dry-run', success=False))

    def test_supervisor_loss_cannot_authorize_retry_or_cleanup(self):
        import os
        import signal
        self.start_probe()
        os.kill(self.supervisor_pid(), signal.SIGKILL)
        self.settle()
        self.assertTrue(self.running_probe_processes())
        for arguments in (('run', '--dry-run'), ('clean', 'sample', '--delete')):
            self.assertIn('cannot confirm computation descendants have stopped', self.cli(*arguments, success=False))

    def test_failed_termination_receipt_publication_stays_unresolved(self):
        self.start_probe()
        receipts = self.work / '.gwf/local-termination'
        receipt = next(path for path in receipts.glob('*.json')
                       if json.loads(path.read_text())['submission'].startswith('sample__right__'))
        receipt.unlink()
        receipt.mkdir()  # Real filesystem failure at durable evidence publication.
        (self.work / 'release').touch()
        self.settle()
        self.assertEqual(self.running_probe_processes(), [])
        for arguments in (('run', '--dry-run'), ('clean', 'sample', '--delete')):
            self.assertIn('cannot confirm computation descendants have stopped', self.cli(*arguments, success=False))

    def test_ordinary_worker_cannot_verify_cancellation_of_recorded_work(self):
        self.start_probe()
        self.worker.kill()
        self.worker.communicate(timeout=10)
        self.wait_for(lambda: not self.running_probe_processes())
        self.executable = GWF
        self.worker = self.start_worker()
        self.executable = GWFLOW
        self.assertIn('cannot verify cancellation identity', self.cli('cancel', 'sample', success=False))
        self.assertIn('cannot confirm computation descendants have stopped',
                      self.cli('run', '--dry-run', success=False))

    def test_cancellation_during_timeout_cleanup_finishes_and_worker_shuts_down(self):
        import os
        import shutil
        import signal
        import sys
        from support import FIXTURES
        shutil.copy(FIXTURES / 'termination_gate.py', self.work)
        self.configure_workflow(right_command=shlex.join([sys.executable, str(self.work / 'termination_gate.py')]))
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace(
            "right = task.target('right', inputs=[], outputs=['same.txt'])",
            "right = task.target('right', inputs=[], outputs=['same.txt'], cores=2, walltime='00:00:02')"))
        marker = self.work / 'capacity-released'
        workflow.write_text(workflow.read_text() +
                            "other = empty_task(inputs=[])\n"
                            "target = other.target('compute', inputs=[], outputs=['out'], cores=2)\n"
                            f"target << {('touch ' + shlex.quote(str(marker)) + '; printf done > out')!r}\n"
                            "other.retain('result', source=target.output('out'), path='out')\n"
                            "gwf.task(other, alias='other')\n")
        def release():
            path = self.work / 'supervisor-pid'
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGCONT)
                except ProcessLookupError:
                    pass
        self.addCleanup(release)
        self.cli('run', 'sample')
        self.wait_for((self.work / 'gate-started').exists)
        self.wait_for((self.work / 'cleanup-held').exists)
        self.cli('run', 'other')
        try:
            self.assertIn('may still be stopping', self.cli('cancel', 'sample'))
            self.cli('cancel', 'sample')
            self.assertIn('queued', self.cli('status', 'other', '--plain'))
            self.assertFalse(marker.exists())
            self.cli('run', 'sample', '--force', success=False)
            self.assertIn('Kept — active work', self.cli('clean', 'sample', '--delete'))
        finally:
            release()
        self.settle()
        self.assertTrue(marker.exists())
        gate_pid = int((self.work / 'gate-pid').read_text())
        self.assertFalse(Path(f'/proc/{gate_pid}').exists())
        self.assertEqual((self.work / 'results/other/out').read_text(), 'done')
        self.assertIn('Retry', self.cli('run', 'sample', '--dry-run'))
        self.worker.terminate()
        self.worker.communicate(timeout=5)
        self.assertEqual(self.worker.returncode, 0)

    def test_cancellation_during_process_launch_reaps_owned_descendants(self):
        import os
        import shutil
        from support import FIXTURES
        self.stop_worker(self.worker)
        self.configure_workflow(right_command=self.probe(escape=True))
        shutil.copy(FIXTURES / 'launch_gate.py', self.work / 'sitecustomize.py')
        environment = {**os.environ, 'PYTHONPATH': str(self.work), 'GWFLOW_LAUNCH_GATE': str(self.work)}
        self.worker = self.start_worker(env=environment)
        release = self.work / 'launch-release'
        self.addCleanup(release.touch)
        self.cli('run')
        self.wait_for((self.work / 'launch-held').exists)
        self.wait_for((self.work / 'held').exists)
        try:
            self.assertIn('may still be stopping', self.cli('cancel', 'sample'))
            self.assertFalse((self.work / 'results/sample/result.txt').exists())
        finally:
            release.touch()
        self.settle()
        self.assert_probe_stopped()
        self.assertIn('Retry', self.cli('run', '--dry-run'))
        self.cli('clean', 'sample', '--delete')
        self.worker.terminate()
        self.worker.communicate(timeout=5)
        self.assertEqual(self.worker.returncode, 0)
