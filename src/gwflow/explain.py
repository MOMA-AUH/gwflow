"""Read-only explanations of the same managed plan consumed by run."""

import click
from gwf import Workflow as GwfWorkflow
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from ._frontend import _submission_guard
from .planning import plan_workflow
from .workflow import Workflow, lifecycle_jobs


@click.command()
@click.option("--details", is_flag=True, help="Show Task attempts and execution jobs.")
@click.option("--force", is_flag=True, help="Preview a forced whole-workflow run.")
@click.argument("task_name", required=False)
@pass_context
def explain(ctx, details, force, task_name):
    """Explain a whole-workflow run without changing managed state."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        raise WorkflowError("gwf explain requires a gwflow.Workflow")
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        plan = plan_workflow(workflow, ctx, force=force)
        if task_name is not None and task_name not in workflow._task_declarations:
            raise WorkflowError(f"Unknown Task name {task_name!r}")
        click.echo("Managed whole-workflow plan")
        for task in plan.tasks:
            if task_name is not None and task.name != task_name:
                continue
            click.echo(f"Task {task.name}: {task.action}; {task.reason}")
            if task.pending:
                for local in task.pending:
                    click.echo(f"  Would submit {task.name}__{local}")
            if details and task.attempt:
                click.echo(f"  Attempt: {task.attempt['attempt']}")
                click.echo(f"  Workspace: {plan.store.workspace(task.attempt)}")
                click.echo(f"  Results: {plan.store.result_dir(task.attempt)}")
                for name, expected in task.attempt["producers"].items():
                    click.echo(f"  Expected producer {name}: {expected}")
                for name, jobs in task.consumers.items():
                    click.echo(f"  Active consumer {name}: " + ", ".join(f"{item.submission} ({item.state})" for item in jobs))
                for local, item in task.submissions.items():
                    click.echo(f"  {task.name}__{local}: {item.submission or 'not submitted'} ({item.state})")
