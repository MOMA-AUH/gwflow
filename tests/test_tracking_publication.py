"""Reuse and admission after an interrupted backend tracking update."""

import json
import os
import shlex
import subprocess
import sys

from support import FIXTURES, LocalBackendTestCase
import test_fresh
import test_managed_recovery


class TrackingPublicationTests(LocalBackendTestCase):
    configure_workflow = test_fresh.FreshAttemptTests.configure_workflow
    slurm_environment = test_managed_recovery.ManagedCoordinationTests.slurm_environment

    def check_interruption(self, backend, phase):
        env = self.slurm_environment() if backend == 'slurm' else os.environ
        def cli(*args, success=True):
            return self.cli('-b', backend, *args, env=env, success=success)

        workflow = self.work / 'workflow.py'
        release = self.work / 'release'
        gate = f'while test ! -e {shlex.quote(str(release))}; do sleep 0.05; done; '
        declaration = workflow.read_text()
        self.assertEqual(declaration.count('echo b >>'), 1)
        workflow.write_text(declaration.replace('echo b >>', gate + 'echo b >>'))
        cli('run', 'a')
        self.finish()
        cli('clean', '--delete', 'a')
        before = json.loads(self.cli_result('-b', backend, 'status', '--json', 'a', env=env).stdout)['tasks'][0]
        self.assertEqual(before['state'], 'reusable')
        attempt = before['attempt_id']
        self.assertFalse((self.work / 'work/a' / attempt).exists())
        retained = self.work / 'results/a/result.txt'
        original_mtime = retained.stat().st_mtime_ns
        document = 'tracked' if phase == 'tracking' else 'status-cache'
        path = self.work / '.gwf' / f'{backend}-backend-{document}.json'
        previous = json.loads(path.read_text())

        try:
            interrupted = subprocess.run([sys.executable, str(FIXTURES / 'interrupted_tracking.py'), phase,
                                          '-b', backend, 'run', 'b'], cwd=self.work, env=env,
                                         capture_output=True, text=True, timeout=30)
            self.assertEqual(interrupted.returncode, 119, interrupted.stdout + interrupted.stderr)
            after = json.loads(self.cli_result('-b', backend, 'status', '--json', 'a', env=env).stdout)['tasks'][0]
            self.assertEqual(after, before)
            self.assertEqual(json.loads(path.read_text()), previous)
            self.assertIn('a: reusable', cli('wait', 'a', '--timeout', '10'))
            self.assertIn('Reuse', cli('run', 'a', '--dry-run'))
            self.assertNoSubmissions(cli('run', 'a'))
            self.assertEqual(retained.read_text(), 'a')
            self.assertEqual(retained.stat().st_mtime_ns, original_mtime)
            if backend == 'slurm':
                forgotten = self.work / 'forgotten-slurm-history'
                forgotten.touch()
                self.assertIn('unresolved submission', cli('run', 'b', success=False))
                forgotten.unlink()
            self.assertIn('Wait', cli('run', 'b', '--dry-run'))
            self.assertNoSubmissions(cli('run', 'b'))
        finally:
            release.touch()
        self.finish()
        self.assertEqual((self.work / 'trace').read_text(), 'a\nb\n')
        if backend == 'slurm':
            self.assertEqual(len((self.work / 'slurm-submitted.jsonl').read_text().splitlines()), 6)

    def test_local_tracking_interruption_preserves_cleaned_task_and_active_admissions(self):
        self.check_interruption('local', 'tracking')

    def test_local_status_cache_interruption_preserves_cleaned_task_and_active_admissions(self):
        self.check_interruption('local', 'cache')

    def test_slurm_tracking_interruption_preserves_cleaned_task_and_active_admissions(self):
        self.check_interruption('slurm', 'tracking')
