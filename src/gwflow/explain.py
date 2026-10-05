"""Read-only explanations of the same managed plan consumed by run."""

import click
from gwf import Workflow as GwfWorkflow
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from ._frontend import _submission_guard
from .inspection import action_line, blockage_line
from .planning import plan_workflow
from .presentation import Report, output_options, selected_rows
from .workflow import Workflow


@click.command()
@output_options
@click.option("--force", is_flag=True, help="Preview a forced whole-workflow run.")
@click.option("--force-task", multiple=True, help="Preview fresh computation for a named whole Task.")
@click.option("--endpoints", is_flag=True, help="Show endpoint Tasks.")
@click.option("-g", "--group", multiple=True, help="Select Tasks by computation-target group.")
@click.argument("targets", nargs=-1)
@pass_context
def explain(ctx, details, force, force_task, targets, endpoints, group, plain, no_truncate):
    """Explain a whole-workflow run without changing managed state."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        raise WorkflowError("gwf explain requires a gwflow.Workflow")
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        plan = plan_workflow(workflow, ctx, force=force, force_tasks=force_task)
        report = Report(plan.store, plain=plain, no_truncate=no_truncate)
        report.line("Managed whole-workflow plan")
        if plan.blocked:
            report.notice(blockage_line(plan))
        for row in selected_rows(workflow, plan, targets=targets, endpoints=endpoints, group=group):
            task = row.task
            report.line(action_line(task))
            if task.pending and not plan.blocked:
                for local in task.pending:
                    report.line(f"  Would submit {task.name}__{local}")
            if details or targets or group:
                report.details(row)
