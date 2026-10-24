"""Isolated compaction acceptance on a live Slurm scheduler and shared storage."""

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import time

from gwflow.lifecycle import Store
from support import FIXTURES, TASK_FACTORY
from validate_compaction_scale import footprint


class Journey:
    def __init__(self, root, queue, timeout):
        self.root, self.timeout = root, timeout
        self.started = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - 60))
        self.evidence = {'command': [sys.executable, *sys.argv], 'python': platform.python_version(),
                         'packages': {name: importlib.metadata.version(name) for name in ('gwflow', 'gwf', 'rich', 'textual')},
                         'snapshots': [], 'checks': [], 'commands': [], 'passed': False}
        root.mkdir(parents=True, exist_ok=False)
        self.work = root / 'workflow'
        self.work.mkdir()
        shutil.copy(FIXTURES / 'job_fault.py', self.work)
        trace = shlex.quote(str(self.work / 'trace'))
        observe = ("import json,os,platform; from pathlib import Path; "
                   f"p=Path({str(self.work)!r}); s=p.stat(); "
                   "(p/'worker-identity.json').write_text(json.dumps(dict(host=platform.node(), device=s.st_dev, inode=s.st_ino)))")
        compute = shlex.join([sys.executable, '-c', observe]) + '; echo sample >> ' + trace + '; echo value > value.txt'
        info = self.work.stat()
        self.evidence['frontend_identity'] = dict(host=platform.node(), device=info.st_dev, inode=info.st_ino)
        resources = dict(queue=queue, cores=1, memory='1g', walltime='00:05:00')
        (self.work / 'workflow.py').write_text(
            TASK_FACTORY + 'from gwflow import Workflow, shell\n'
            f'gwf = Workflow(defaults={resources!r})\n'
            'task = empty_task(inputs=[])\n'
            "target = task.target('compute', inputs=[], outputs=['value.txt'])\n"
            f'target << {compute!r}\n'
            "task.retain('value', source=target.output('value.txt'), path='value.txt')\n"
            "sample = gwf.task(task, alias='sample')\n"
            "task = empty_task(inputs=[sample.outputs['value']])\n"
            "target = task.target('compute', inputs=task.inputs, outputs=['value.txt'])\n"
            f"target << shell({'echo consumer >> ' + trace + '; cat {source} > value.txt'!r}, source=sample.outputs['value'])\n"
            "task.retain('value', source=target.output('value.txt'), path='value.txt')\n"
            "gwf.task(task, alias='consumer')\n")
        (self.work / '.gwfconf.json').write_text(json.dumps({'backend': 'slurm'}))
        self.inject()

    def inject(self, **options):
        (self.work / 'injection.json').write_text(json.dumps(options))
        (self.work / 'job-fault.json').write_text(json.dumps({'compaction_fault': 'error-during-deletion'}))

    def command(self, arguments):
        result = subprocess.run(arguments, cwd=self.work, capture_output=True, text=True, timeout=self.timeout)
        item = dict(command=arguments, exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
        self.evidence['commands'].append(item)
        with (self.root / 'transcript.jsonl').open('a') as stream:
            stream.write(json.dumps(item) + '\n')
        assert result.returncode == 0, item
        return result.stdout + result.stderr

    def cli(self, *arguments):
        return self.command([sys.executable, str(FIXTURES / 'live_slurm_compaction.py'), *arguments])

    def accepted(self):
        return [json.loads(line) for line in (self.work / 'accepted.jsonl').read_text().splitlines()]

    def accounting(self):
        jobs = ','.join(item['job'] for item in self.accepted())
        output = self.command(['sacct', '-X', '-n', '-P', '-S', self.started, '-j', jobs,
                               '--format=JobIDRaw,JobName%150,State,ExitCode,NodeList'])
        self.evidence['accounting'] = output
        return {line.split('|')[0]: line.split('|')[2].split()[0] for line in output.splitlines() if line}

    def wait(self, predicate):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(3)
        raise TimeoutError('Slurm journey timed out; inspect accepted.jsonl and transcript.jsonl')

    def finish_new(self, previous):
        jobs = {item['job'] for item in self.accepted()[previous:]}
        assert jobs
        def complete():
            states = self.accounting()
            failed = {job: states[job] for job in jobs if states.get(job) in ('FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'CANCELLED')}
            assert not failed, failed
            return all(states.get(job) == 'COMPLETED' for job in jobs)
        self.wait(complete)

    def snapshot(self, stage):
        snapshot = footprint(self.work)
        snapshot['stage'] = stage
        self.evidence['snapshots'].append(snapshot)
        for item in snapshot['attempts'].values():
            assert item['phase'] == 'compacted' and item['files'] == 2 and item['directories'] == 1, item
        assert 'transient_conversion' not in snapshot['categories']

    def run(self):
        try:
            self.evidence['slurm_version'] = self.command(['sbatch', '--version']).strip()
            self.evidence['filesystem'] = self.command(['df', '-T', str(self.root)])
            # A real failed completion leaves installed outputs, but afterok
            # prevents the already-submitted consumer from executing.
            self.inject(fault=True)
            self.cli('run')
            completion = next(item for item in self.accepted() if item['name'].startswith('sample__gwflow_complete__'))
            consumer = next(item for item in self.accepted() if item['name'].startswith('consumer__gwflow_prepare__'))
            assert completion['job'] in consumer['dependencies']
            self.wait(lambda: self.accounting().get(completion['job']) == 'FAILED')
            assert (self.work / 'results/sample/value.txt').read_text() == 'value\n'
            assert (self.work / 'trace').read_text().splitlines() == ['sample']
            self.evidence['blocked_queue'] = self.command(['squeue', '--noheader', '-j', consumer['job'], '-o', '%i|%T|%r|%E'])
            cancelled = self.accounting().get(consumer['job']) == 'CANCELLED'
            assert cancelled or 'DependencyNeverSatisfied' in self.evidence['blocked_queue']
            self.evidence['worker_identity'] = json.loads((self.work / 'worker-identity.json').read_text())
            assert self.evidence['worker_identity']['inode'] == self.evidence['frontend_identity']['inode']
            store = Store(self.work)
            before = store.recorded_current('sample')
            state_path = next((self.work / '.gwf/gwflow').glob('owners/*/tasks/sample/attempts/*/record-state.json'))
            assert json.loads(state_path.read_text())['phase'] == 'consolidating'
            self.evidence['failed_footprint'] = footprint(self.work)
            info = (self.work / 'results/sample/value.txt').stat()
            self.evidence['result_before_retry'] = dict(device=info.st_dev, inode=info.st_ino, mtime_ns=info.st_mtime_ns)
            assert 'compaction' in self.cli('status', '--plain').lower()
            self.evidence['checks'].append('post-publication deletion failure blocks real afterok dependency')
            # Some sites cancel invalid dependencies automatically. Others keep
            # them pending; cancel only this isolated consumer in that case.
            if not cancelled:
                self.cli('cancel', 'consumer')
            def consumers_inactive():
                states = self.accounting()
                return all(states.get(item['job'], '').startswith('CANCELLED')
                           for item in self.accepted() if item['name'].startswith('consumer__'))
            self.wait(consumers_inactive)
            self.inject(late_ack=True)
            previous = len(self.accepted())
            assert 'compaction' in self.cli('run', '--dry-run').lower()
            self.cli('run')
            self.finish_new(previous)
            after = store.recorded_current('sample')
            assert after == before
            info = (self.work / 'results/sample/value.txt').stat()
            self.evidence['result_after_retry'] = dict(device=info.st_dev, inode=info.st_ino, mtime_ns=info.st_mtime_ns)
            assert self.evidence['result_before_retry'] == self.evidence['result_after_retry']
            late = json.loads((self.work / 'before-late-ack.json').read_text())
            current = json.loads(state_path.read_text())
            assert all(current['records'][name] == value for name, value in late['records'].items())
            assert len(current['records']) > len(late['records'])
            assert current['lock'] == late['lock']
            self.snapshot('ordinary-retry-and-late-ack')
            self.evidence['checks'].append('ordinary run retries completion without computation or result replacement; late acknowledgement preserved')
            self.inject()
            trace = (self.work / 'trace').read_text()
            for cycle in range(2):
                (self.work / 'results/sample/value.txt').unlink()
                assert 'Repair' in self.cli('run', '--dry-run', 'sample')
                previous = len(self.accepted())
                self.cli('run', 'sample')
                self.finish_new(previous)
                assert store.recorded_current('sample')['attempt'] == before['attempt']
                assert (self.work / 'trace').read_text() == trace
                self.snapshot(f'repair-{cycle + 1}')
            self.cli('clean', '--delete')
            self.cli('clean', '--delete')
            self.snapshot('repeated-cleanup')
            assert 'Reuse' in self.cli('run', '--dry-run')
            assert completion['name'] in self.cli('logs', 'sample', '--history', '--details')
            self.evidence['checks'].append('repeated Repair and cleanup preserve bound, computation and historical logs')
            self.evidence['accepted'] = self.accepted()
            self.evidence['passed'] = True
        finally:
            (self.root / 'summary.json').write_text(json.dumps(self.evidence, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--queue', default='short')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    Journey(args.root.resolve(), args.queue, args.timeout).run()


if __name__ == '__main__':
    main()
