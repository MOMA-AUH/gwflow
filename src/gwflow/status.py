"""Compact managed Task status."""

import click
from gwf.core import pass_context

from ._frontend import _submission_guard, load_workflow
from .planning import plan_workflow
from .presentation import STATES, Report, output_options, selected_rows, task_groups, task_order
from .selection import task_selection


class _StatusChoice(click.Choice):
    def convert(self, value, param, ctx):
        return super().convert("canceled" if value == "cancelled" else value, param, ctx)


def inspection_command(plain, no_truncate):
    """Retain invocation context and selection; attention supplies one state."""
    root = click.get_current_context().find_root()
    command = ["gwflow"]
    for name, option in (("file", "--file"), ("backend", "--backend"), ("verbose", "--verbose")):
        if root.get_parameter_source(name) == click.core.ParameterSource.COMMANDLINE:
            command.extend((option, str(root.params[name])))
    if root.get_parameter_source("no_color") == click.core.ParameterSource.COMMANDLINE:
        command.append("--no-color" if root.params["no_color"] else "--use-color")
    command.extend(("status", "*"))
    if plain:
        command.append("--plain")
    if no_truncate:
        command.append("--no-truncate")
    return command


@output_options
@click.command(name="status")
@click.argument("selectors", nargs=-1)
@click.option("-s", "--status", "statuses", multiple=True, type=_StatusChoice(STATES))
@pass_context
def status(ctx, selectors, statuses, details, plain, no_truncate):
    """Show a grouped overview or selected Task names and quoted globs."""
    workflow = load_workflow(ctx)
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        names = task_selection(workflow._task_declarations, selectors)
        plan = plan_workflow(workflow, ctx, selectors=names)
        report = Report(plan.store, plain=plain, no_truncate=no_truncate)
        rows = selected_rows(workflow, plan, names=names, statuses=statuses)
        total = len(workflow._task_declarations)
        if not selectors and not details:
            report.overview(task_groups(workflow, plan), rows, total)
            report.attention(rows, inspection_command(plain, no_truncate))
        else:
            report.status(rows, total, expand=details)
        report.workflow_notices(task_order(workflow, plan), rows)
