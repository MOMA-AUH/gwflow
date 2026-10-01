"""Read-only explanations of the same managed plan consumed by run."""

import click
from gwf import Workflow as GwfWorkflow
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from ._frontend import _submission_guard
from .inspection import action_line, blockage_line, task_details
from .planning import plan_workflow
from .workflow import Workflow


@click.command()
@click.option("--details", is_flag=True, help="Show Task attempts and execution jobs.")
@click.option("--force", is_flag=True, help="Preview a forced whole-workflow run.")
@click.option("--force-task", multiple=True, help="Preview fresh computation for a named whole Task.")
@click.argument("task_name", required=False)
@pass_context
def explain(ctx, details, force, force_task, task_name):
    """Explain a whole-workflow run without changing managed state."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        raise WorkflowError("gwf explain requires a gwflow.Workflow")
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        plan = plan_workflow(workflow, ctx, force=force, force_tasks=force_task)
        if task_name is not None and task_name not in workflow._task_declarations:
            raise WorkflowError(f"Unknown Task name {task_name!r}")
        click.echo("Managed whole-workflow plan")
        if plan.blocked:
            click.echo(blockage_line(plan))
        for task in plan.tasks:
            if task_name is not None and task.name != task_name:
                continue
            click.echo(action_line(task))
            if task.pending and not plan.blocked:
                for local in task.pending:
                    click.echo(f"  Would submit {task.name}__{local}")
            if details:
                for line in task_details(plan.store, task):
                    click.echo(f"  {line}")
