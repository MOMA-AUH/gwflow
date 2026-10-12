"""The controlled persistence and submission boundary for managed workflows."""

import click
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from ._frontend import _submission_guard, load_workflow
from .admission import SubmissionOutcome
from .planning import plan_workflow
from .presentation import Report, output_options, selected_rows
from .submission import submit_plan


@output_options
@click.command()
@click.argument("targets", nargs=-1)
@click.option("-d", "--dry-run", is_flag=True, help="Preview the execution plan without submitting jobs.")
@click.option("-f", "--force", is_flag=True, help="Start fresh attempts for the whole workflow.")
@click.option("--force-task", multiple=True, help="Start a fresh attempt for a named whole Task.")
@pass_context
def run(ctx, targets, dry_run, force, force_task, details, plain, no_truncate):
    """Run the managed workflow, or preview it with --dry-run."""
    workflow = load_workflow(ctx)
    if targets:
        raise WorkflowError("gwflow supports whole-workflow run only; selectors are unsupported")
    with _submission_guard(ctx.working_dir):
        plan = plan_workflow(workflow, ctx, force=force, force_tasks=force_task)
        report = Report(plan.store, plain=plain, no_truncate=no_truncate)
        report.plan(plan, selected_rows(workflow, plan), expand=details, preview=dry_run)
        outcomes = {(task.name, local): SubmissionOutcome(task.name, local)
                    for task in plan.tasks for local in task.pending}
        try:
            submit_plan(plan, workflow, ctx, dry_run=dry_run, outcomes=outcomes)
        except BaseException:
            if not dry_run and not plan.blocked:
                report.submissions(outcomes.values(), interrupted=True, details=details)
            raise
        if not dry_run:
            report.submissions(outcomes.values(), details=details)
