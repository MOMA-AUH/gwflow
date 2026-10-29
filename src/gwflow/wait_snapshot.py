"""Inspect one waiting scope in a disposable process."""

import json
from pathlib import Path
import signal
import sys

import click
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from .admission import waiting_observations
from ._frontend import _submission_guard, load_workflow
from .planning import definition_change, input_metadata_changes, plan_workflow
from .presentation import task_row


@click.command(name='wait-snapshot')
@click.option('--snapshot-file', 'destination', type=click.Path(path_type=Path), required=True)
@click.argument('selectors', nargs=-1)
@pass_context
def capture(ctx, destination, selectors):
    sys.pycache_prefix = str(destination.parent / 'pycache')
    sys.dont_write_bytecode = True
    request_path = destination.parent / "request.json"
    awaited = json.loads(request_path.read_text()) if request_path.exists() else None
    workflow = load_workflow(ctx)
    if awaited is not None:
        missing = set(awaited) - workflow._task_declarations.keys()
        if missing:
            raise WorkflowError("Awaited Tasks removed: " + ", ".join(sorted(missing)))
        selectors = tuple(awaited)
    with _submission_guard(ctx.working_dir), waiting_observations() as scheduler_errors:
        plan = plan_workflow(workflow, ctx, selectors=selectors)
        observations = {task.name: task for task in plan.tasks}
        for task in plan.tasks:
            if awaited is None or task.name in awaited:
                validate_attempt(plan.store, task, observations)
        tasks = [observe_task(task) for task in plan.tasks if awaited is None or task.name in awaited]
        if awaited is not None:
            for task in tasks:
                if task['attempt'] != awaited[task['name']]:
                    raise WorkflowError(f"Awaited Task attempt replaced: {task['name']}")
    destination.write_text(json.dumps({'tasks': tasks, 'scheduler_errors': sorted(set(scheduler_errors))}))


def validate_attempt(store, task, observations):
    """Uncertainty must not hide a decisive invalidation of the awaited work."""
    if task.attempt is None:
        return
    conflicts = [local for local, item in task.submissions.items() if item.identity_conflict]
    if conflicts:
        raise WorkflowError(f"Conflicting saved and tracked job IDs for {task.name}: " + ', '.join(conflicts))
    initializing = store.read(task.attempt, 'ready.json', 'ready') is None
    change = definition_change(store, task, observations, force_option=None, initializing=initializing)
    if change:
        raise WorkflowError(f"Awaited Task {task.name} invalidated: {change[1]}")
    if task.action != 'deferred' and (changes := input_metadata_changes(store, task)):
        raise WorkflowError(f"Awaited Task {task.name} invalidated: input metadata changed: " + '; '.join(changes))


def observe_task(task):
    failures = [local for local, item in task.submissions.items() if item.state in ('failed', 'cancelled')]
    unknown = any(item.state == 'uncertain' for item in task.submissions.values())
    reason = task.reason
    if failures:
        reason = 'Jobs failed or canceled: ' + ', '.join(failures) + '; ' + reason
    return {'name': task.name, 'attempt': task.attempt['attempt'] if task.attempt else None,
            'state': task_row(task).state, 'reason': reason,
            'complete': task.action == 'reuse' and not failures,
            'pending': not failures and (task.action == 'active' or
                                        task.reason_code == 'submission_unresolved' and unknown)}


if __name__ == '__main__':
    from .cli import main

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    main.add_command(capture)
    main()
