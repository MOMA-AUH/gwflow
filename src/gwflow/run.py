"""The controlled persistence and submission boundary for managed workflows."""

import click
from gwf.core import pass_context

from ._frontend import _submission_guard, load_workflow
from .admission import SubmissionOutcome
from .planning import plan_workflow
from .presentation import Report, output_options, selected_rows
from .submission import submit_plan


@output_options
@click.command()
@click.argument("selectors", nargs=-1)
@click.option("-d", "--dry-run", is_flag=True, help="Preview the execution plan without submitting jobs.")
@click.option("-f", "--force", is_flag=True, help="Start fresh attempts for directly selected Tasks.")
@pass_context
def run(ctx, selectors, dry_run, force, details, plain, no_truncate):
    """Run Task names or quoted globs with their required upstream Tasks."""
    workflow = load_workflow(ctx)
    with _submission_guard(ctx.working_dir):
        plan = plan_workflow(workflow, ctx, selectors=selectors, force=force)
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
