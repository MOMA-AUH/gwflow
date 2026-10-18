"""Record an operator's whole-Task inactivity assertion without submitting work."""

from contextlib import ExitStack
import shlex

import click
from gwf.backends import create_backend
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from . import admission
from .activity import activity
from .diagnostics import report_error
from ._frontend import _submission_guard, load_workflow, observation_deadline
from .lifecycle import Store
from .presentation import inspection_command
from .workflow import lifecycle_jobs


@click.command()
@click.argument("task")
@pass_context
def recover(ctx, task):
    """Resolve uncertain submissions in TASK's current attempt.

    Invoking this command asserts that the whole Task is inactive. Investigate
    its computation, preparation, and completion jobs first: cancellation alone
    does not prove they have stopped. Known queued or running jobs prevent
    recovery. No jobs are submitted or canceled and no success is inferred.
    """
    workflow = load_workflow(ctx)
    with _submission_guard(ctx.working_dir):
        store = Store.for_workflow(workflow)
        attempts = {attempt["task"]: attempt for attempt in store.recorded_tasks()}
        if task not in set(workflow._task_declarations) | set(attempts):
            raise WorkflowError(f"Unknown Task {task!r}; recover requires one exact Task name")
        attempt = attempts.get(task)
        if attempt is None:
            click.echo(f"Task {task}: no recorded attempt; no unresolved submissions")
            return
        # Missing or malformed current intents require investigation, not repair
        # through an inactivity assertion.
        for local in lifecycle_jobs(attempt["executions"]):
            admission.read_intent(store, attempt, local)
        with activity("Waiting for scheduler..."), ExitStack() as resources:
            try:
                with observation_deadline():
                    backend = resources.enter_context(create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config))
            except Exception as error:
                report_error(error, f"Observing Task {task}")
                click.echo(f"Backend observations unavailable: {error}; relying on the whole-Task inactivity assertion.")
                backend = None
            observations = admission.observe_submissions(store, attempt, backend, ctx.backend,
                                                        tolerate_state_errors=True)
            resolved = admission.resolve_submissions(store, attempt, observations)
        click.echo(f"Task {task}: current attempt {attempt['attempt']}")
        for item in resolved:
            click.echo(f"  Resolved submission {item.submission}; whole-Task inactivity asserted")
        if not resolved:
            click.echo("  No unresolved submissions")
        preview = shlex.join(inspection_command("run", task, "--dry-run"))
        click.echo(f"No jobs submitted. Inspect '{preview}' before ordinary run.")
