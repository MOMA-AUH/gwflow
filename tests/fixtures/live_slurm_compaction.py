"""Instrument real Slurm submissions; optionally fault one completion worker.

All acceptance, dependency scheduling, and accounting use the installed Slurm
backend. The only substitutions are the existing worker fault wrapper and an
optional delay returning one accepted job ID to the frontend.
"""

import json
from pathlib import Path
import shlex
import sys
import time

from gwf.backends.slurm import SlurmOps
from gwflow.cli import main

work = Path.cwd()
options = json.loads((work / 'injection.json').read_text())
original = SlurmOps.submit_target


def submit(self, target, dependencies):
    if target.name.startswith('sample__gwflow_complete__') and options.get('fault'):
        arguments = shlex.split(target.spec.removeprefix('exec '))
        target.spec = 'exec ' + shlex.join([sys.executable, str(work / 'job_fault.py'), str(work), *arguments[1:]])
    job = original(self, target, dependencies)
    with (work / 'accepted.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(job=job, name=target.name, dependencies=dependencies)) + '\n')
    if target.name.startswith('sample__gwflow_complete__') and options.get('late_ack'):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            for path in (work / '.gwf/gwflow').glob('owners/*/tasks/sample/attempts/*/record-state.json'):
                state = json.loads(path.read_text())
                if state['phase'] == 'compacted':
                    admission = target.name.rsplit('__', 1)[1]
                    assert f'admissions/{admission}/ack.json' not in state['records']
                    (work / 'before-late-ack.json').write_text(json.dumps(state))
                    return job
            time.sleep(1)
        raise RuntimeError('Timed out holding real Slurm acknowledgement')
    return job


SlurmOps.submit_target = submit
main(sys.argv[1:])
