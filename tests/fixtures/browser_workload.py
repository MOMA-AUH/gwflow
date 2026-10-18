"""Captured-observation workload, independent of workflow and backend inspection."""

import time

from gwflow.browser import StatusBrowser


class Snapshot(dict):
    """Allow resource checks to observe collection without keeping data alive."""


def captured_snapshot():
    tasks = []
    states = ['reusable'] * 14 + ['running'] * 4 + ['failed'] * 6
    states += ['pending', 'repairable', 'queued', 'blocked'] * 19
    for index, state in enumerate(states):
        name = 'mapping_' + 'long_reference_' * 8 + f'__sample_{index:03d}'
        jobs = [dict(local=f'job_{job:02d}', name=f'{name}__job_{job:02d}',
                     state='completed' if state == 'reusable' else 'pending',
                     observed_state='completed' if state == 'reusable' else 'pending',
                     requires_repetition=False, submission=None, backend_id=None,
                     logs={'stdout': f'/captured/logs/{name}/{job}.stdout',
                           'stderr': f'/captured/logs/{name}/{job}.stderr'}) for job in range(10)]
        tasks.append(dict(name=name, state=state, completed=10 if state == 'reusable' else 0, total=10,
                          detail='', jobs=jobs, next_action='Reuse' if state == 'reusable' else 'Inspect',
                          reason='Captured workload', diagnostics='Captured attempt and paths\n' * 20,
                          commands=[f'gwflow run {name} --dry-run', f'gwflow run {name}', f'gwflow cancel {name}']))
    return Snapshot(finished=time.time(), total=100, tasks=tasks,
                    groups=[dict(key='mapping@captured.main', label='mapping',
                                 members=[task['name'] for task in tasks[:24]]),
                            dict(key='large@captured.other', label='large_' + 'group_' * 25,
                                 members=[task['name'] for task in tasks[24:]])],
                    notices='Workflow blocked: sample_099; inspect its diagnostics before running.')


class CapturedBrowser(StatusBrowser):
    """Feed captured observations through the same completed-inspection interface."""

    def __init__(self, *, snapshot_factory=captured_snapshot, status_interval=0.1, **options):
        super().__init__(([], ()), **options)
        self.acquisitions = 0
        self.snapshot_factory = snapshot_factory
        self.status_interval = status_interval

    async def action_refresh(self):
        self.acquisitions += 1
        await self.accept_inspection(self.snapshot_factory())

    async def on_mount(self, event):
        event.prevent_default()
        await self.action_refresh()
        self.set_interval(self.status_interval, self.inspect_progress)
