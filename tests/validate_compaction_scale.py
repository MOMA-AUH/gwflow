"""Measure installed completion, retry/recovery history, Repair and cleanup.

Run with an installed candidate. All workflows and raw probe reports stay below
--root. Counts include directory roots and never follow symlinks.
"""

import argparse
from collections import Counter
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import stat
import subprocess
import sys
import time
import unittest

from gwflow.lifecycle import Store
from support import FIXTURES, GWFLOW, LocalBackendTestCase, TASK_FACTORY
from test_graphs import TaskGraphTests


def footprint(work):
    """Disjoint physical categories; attempt directories count towards the bound."""
    attempts = {}
    roots = {path.parent for path in (work / '.gwf/gwflow').rglob('record-state.json')}
    counts = {}
    for path in work.rglob('*'):
        relative = path.relative_to(work)
        parts = relative.parts
        attempt = next((root for root in roots if path == root or root in path.parents), None)
        if attempt:
            category = 'attempt_metadata'
            if path.name.startswith('.pending-'):
                category = 'transient_conversion'
        elif parts[:2] == ('.gwf', 'logs'):
            category = 'logs_and_aliases'
        elif parts[0] in ('work', 'results'):
            category = 'data'
        elif parts[:2] == ('.gwf', 'gwflow') and 'tasks' in parts:
            category = 'task_bookkeeping'
        elif parts[0] == '.gwf':
            category = 'workflow_bookkeeping'
        else:
            category = 'validation_fixture'
        info = path.lstat()
        kind = ('files' if stat.S_ISREG(info.st_mode) else 'directories' if stat.S_ISDIR(info.st_mode)
                else 'symlinks' if stat.S_ISLNK(info.st_mode) else 'other')
        counts.setdefault(category, Counter())[kind] += 1
        if kind == 'files':
            counts[category]['bytes'] += info.st_size
        if attempt:
            attempts.setdefault(str(attempt.relative_to(work)), Counter())[kind] += 1
    for root in roots:
        item = attempts[str(root.relative_to(work))]
        state = json.loads((root / 'record-state.json').read_text())
        item['phase'] = state['phase']
        item['logical_records'] = len(state['records']) if state['records'] is not None else None
        for name in ('record-state.json', 'records.lock'):
            info = (root / name).stat()
            item[name] = dict(device=info.st_dev, inode=info.st_ino, bytes=info.st_size)
    return {'categories': counts, 'attempts': attempts}


class ScaleCase(LocalBackendTestCase):
    workers = 8
    settle = TaskGraphTests.settle
    inject = TaskGraphTests.inject

    def make_workdir(self):
        self.work_root.mkdir()
        return self.work_root

    def configure_workflow(self):
        trace = shlex.quote(str(self.work / 'trace'))
        failing = shlex.quote(str(self.work / 'fail'))
        source = TASK_FACTORY + 'from gwflow import Workflow\ngwf = Workflow()\n'
        for task in range(self.task_count):
            source += 'task = empty_task(inputs=[])\n'
            for target in range(self.target_count):
                command = f'echo {task}:{target} >> {trace}; test ! -f {failing}; echo value > value.txt'
                source += (f"target = task.target('step{target}', inputs=[], outputs=['value.txt'])\n"
                           f'target << {command!r}\n')
            source += ("task.retain('value', source=target.output('value.txt'), path='value.txt')\n"
                       f"gwf.task(task, alias='sample{task}')\n")
        (self.work / 'workflow.py').write_text(source)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail('Timed out waiting for scale workers')

    def cli_result(self, *args, success=True, env=None):
        result = subprocess.run([GWFLOW, *args], cwd=self.work, env=env,
                                capture_output=True, text=True, timeout=300)
        with (self.work / 'commands.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(command=[GWFLOW, *args], exit=result.returncode,
                                         stdout=result.stdout, stderr=result.stderr)) + '\n')
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def run_complete(self, *options):
        self.cli('run', *options)
        self.settle()
        # Historical failed jobs remain failed in the local scheduler. Require
        # Reuse of the selected current attempt instead of rewriting that history.
        self.assertIn('Reuse', self.cli('run', '--dry-run', *options))

    def snapshot(self, stage, *, compacted=True):
        snapshot = footprint(self.work)
        snapshot['stage'] = stage
        if compacted:
            self.assertEqual(len(snapshot['attempts']), self.task_count)
            for attempt in snapshot['attempts'].values():
                self.assertEqual(attempt['phase'], 'compacted')
                self.assertEqual(attempt['files'], 2)
                self.assertEqual(attempt['directories'], 1)
            self.assertNotIn('transient_conversion', snapshot['categories'])
        self.evidence['snapshots'].append(snapshot)
        return snapshot

    def measure(self, stage):
        reports = {}
        for arguments in (('status', '--plain'), ('run', '--dry-run')):
            report = self.work / (stage + '-' + arguments[0] + '.json')
            command = [sys.executable, str(FIXTURES / 'planning_probe.py'), str(report), *arguments]
            result = subprocess.run(command, cwd=self.work, capture_output=True, text=True, timeout=300,
                                    env={**os.environ, 'GWFLOW_PROBE_MEMORY': '1'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, self.cli_result(*arguments).stdout)
            measured = json.loads(report.read_text())
            self.assertTrue(measured['document_decodes'])
            # Eviction can require a new observation when there are >8 attempts.
            # A single large attempt must never decode once per logical lookup.
            if self.task_count == 1:
                self.assertEqual(set(measured['document_decodes'].values()), {1})
            self.assertLessEqual(measured['peak_cached_documents'], 8)
            self.assertLessEqual(measured['peak_opened_descriptors'], 67)
            reports[' '.join(arguments)] = {'command': command, **measured}
        self.evidence.setdefault('probes', {})[stage] = reports

    def historical_records(self):
        store = Store(self.work)
        records = store.records(store.recorded_current('sample0'))
        history = {}
        def visit(namespace):
            for child in records.names(namespace):
                name = namespace + '/' + child
                if name.endswith('.json'):
                    history[name] = records.read(name)
                else:
                    visit(name)
        with records.access():
            for namespace in ('admissions', 'recoveries'):
                visit(namespace)
        return history

    def assert_history(self, expected, stage):
        actual = self.historical_records()
        self.assertEqual({name: actual.get(name) for name in expected}, expected)
        self.evidence.setdefault('history_preservation', []).append(dict(
            stage=stage, records=len(expected),
            recoveries=sum(name.startswith('recoveries/') for name in expected),
            sha256=hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
            unchanged=True))

    def runTest(self):
        self.evidence = dict(targets_per_task=self.target_count, tasks=self.task_count,
                             retries=self.retries, recoveries=self.recoveries, snapshots=[])
        try:
            for _ in range(self.recoveries):
                self.cli('-b', 'recovery_fixture', 'run', env=self.inject(reject_prefix='sample0__gwflow_prepare'), success=False)
                self.assertIn('Resolved', self.cli('recover', 'sample0'))
            if self.retries:
                (self.work / 'fail').touch()
                for _ in range(self.retries):
                    self.cli('run')
                    self.settle()
                (self.work / 'fail').unlink()
            history = {}
            if self.retries or self.recoveries:
                self.snapshot('before-success', compacted=False)
                history = self.historical_records()
                self.assertEqual(sum(name.startswith('recoveries/') for name in history), self.recoveries)
            self.run_complete()
            self.assert_history(history, 'success')
            self.snapshot('success')
            self.measure('success')
            store = Store(self.work)
            attempt = store.recorded_current('sample0')
            trace = (self.work / 'trace').read_text()
            for cycle in range(2):
                (self.work / 'results/sample0/value.txt').unlink()
                self.assertIn('Repair', self.cli('run', '--dry-run', 'sample0'))
                self.run_complete('sample0')
                current = store.recorded_current('sample0')
                self.assertEqual(current['attempt'], attempt['attempt'])
                self.assertEqual(current['executions'], attempt['executions'])
                self.assertEqual((self.work / 'trace').read_text(), trace)
                self.snapshot(f'repair-{cycle + 1}')
                self.assert_history(history, f'repair-{cycle + 1}')
            for cycle in range(2):
                self.cli('clean', '--delete')
                self.snapshot(f'cleanup-{cycle + 1}')
                self.assert_history(history, f'cleanup-{cycle + 1}')
            self.assertIn('Reuse', self.cli('run', '--dry-run'))
            self.measure('after-cleanup')
        finally:
            (self.work / 'evidence.json').write_text(json.dumps(self.evidence, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--targets', type=int, nargs='*', default=[1, 32, 128])
    parser.add_argument('--histories', type=int, nargs='+', default=[4, 16, 64])
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    cases = []
    variants = [(f'targets-{n}', n, 1, 0, 0) for n in args.targets]
    variants += [(f'history-{n}', 1, 1, n, n) for n in args.histories]
    variants += [('cache-eviction-16-tasks', 1, 16, 0, 0)]
    for name, targets, tasks, retries, recoveries in variants:
        case = ScaleCase()
        case.work_root = root / name
        case.target_count, case.task_count = targets, tasks
        case.retries, case.recoveries = retries, recoveries
        cases.append(case)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(cases))
    summary = dict(command=[sys.executable, *sys.argv], python=platform.python_version(),
                   packages={name: importlib.metadata.version(name) for name in ('gwflow', 'gwf', 'rich', 'textual')},
                   tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   cases=[dict(path=str(case.work_root), **getattr(case, 'evidence', {})) for case in cases])
    (root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
