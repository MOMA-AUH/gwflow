"""Preview or delete owned work under the shared frontend guard."""

import click
from gwf import Workflow as GwfWorkflow
from gwf.backends import create_backend
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from . import cleanup
from ._frontend import _submission_guard
from .lifecycle import Store, declarations
from .workflow import Workflow


def display(store, observation):
    attempt = observation.attempt
    click.echo(f"Task {attempt['task']} attempt {attempt['attempt']}: {observation.action}; {observation.reason}")
    click.echo(f"  Workspace: {store.workspace(attempt)}")
    click.echo(f"  Staging group: {store.locations['staging'] / attempt['attempt']}")
    if observation.explicit:
        click.echo("  Consequences: removes successful intermediate progress, diagnostics within work, and repair sources; "
                   "incomplete computation will need a fresh attempt. Current results and logs are preserved.")
    for path in observation.directories:
        if path != str(store.workspace(attempt)):
            click.echo(f"  Staging: {path}")


@click.command("clean-work")
@click.option("--delete", is_flag=True, help="Delete eligible owned work without an interactive prompt.")
@click.option("--task", multiple=True, help="Limit completed-work cleanup to exact Task names.")
@click.option("--attempt", "attempt_ids", multiple=True, help="Select exact recorded inactive attempts, giving up retry progress and repair sources.")
@pass_context
def clean_work(ctx, delete, task, attempt_ids):
    """Preview completed work or explicitly selected inactive attempts."""
    if task and attempt_ids:
        raise WorkflowError("Cannot combine --task and --attempt")
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        raise WorkflowError("gwf clean-work requires a gwflow.Workflow")
    with _submission_guard(ctx.working_dir):
        store = Store.for_workflow(workflow)
        for name, declaration in workflow._task_declarations.items():
            declarations(declaration, store, workflow)
            store.current(name, workflow._result_dirs[name])
        attempts = store.recorded_attempts()
        known = set(workflow._task_declarations) | {attempt["task"] for attempt in attempts}
        if set(task) - known:
            raise WorkflowError("Unknown Task names for --task: " + ", ".join(sorted(set(task) - known)))
        unknown = set(attempt_ids) - {attempt["attempt"] for attempt in attempts}
        if unknown:
            raise WorkflowError("Unknown attempts for --attempt: " + ", ".join(sorted(unknown)))
        selected = [attempt for attempt in attempts if attempt["attempt"] in attempt_ids] if attempt_ids else [
            attempt for attempt in attempts if not task or attempt["task"] in task]
        with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
            observations = [cleanup.observe(store, attempt, backend, ctx.backend, explicit=bool(attempt_ids)) for attempt in selected]
        click.echo("Work cleanup" if delete else "Cleanup preview; use --delete to remove eligible work")
        if not observations:
            click.echo("No recorded attempts selected")
        if delete and any(item.action == "blocked" for item in observations):
            for item in observations:
                display(store, item)
            raise WorkflowError("Cleanup refused for ownership or activity")
        if delete:
            with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
                observations = [cleanup.remove(store, item, backend, ctx.backend) if item.action == "eligible" else item
                                for item in observations]
        for item in observations:
            display(store, item)
        if delete and any(item.action == "blocked" for item in observations):
            raise WorkflowError("Cleanup refused after rechecking ownership or activity")
