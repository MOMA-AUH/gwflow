"""Compact managed Task status."""

from fnmatch import fnmatchcase
import os
import shlex
import sys

import click
from gwf.core import pass_context

from ._frontend import _submission_guard, load_workflow
from .planning import plan_workflow
from .presentation import STATES, Report, inspection_command, output_options, selected_rows, task_groups, task_order
from .selection import task_selection
from .status_json import StatusCommand, status_document
from .terminal import supports_decoration, supports_keyboard


class _StatusChoice(click.Choice):
    def convert(self, value, param, ctx):
        return super().convert("canceled" if value == "cancelled" else value, param, ctx)


def status_selection(workflow, selectors):
    """Explain display labels without adding a group-selector language."""
    names = workflow._task_declarations
    try:
        return task_selection(names, selectors)
    except click.UsageError as error:
        hints = []
        for group in task_groups(workflow):
            if group.label not in selectors or any(fnmatchcase(name, group.label) for name in names):
                continue
            hints.append(f"{group.label!r} is a group display label; selectors match Task names.")
            hints.append("Member examples: " + ", ".join(group.members[:5]) + (
                f" ({len(group.members) - 5} more)" if len(group.members) > 5 else ""))
            pattern = group.prefix + "__*"
            if {name for name in names if fnmatchcase(name, pattern)} == set(group.members):
                hints.append("Select these Tasks: " + shlex.join(inspection_command("status", pattern, "--static")))
        hints.append("List Task names: " + shlex.join(inspection_command("status", "*", "--static")))
        raise click.UsageError(error.message + "\n" + "\n".join(hints)) from error


@output_options
@click.command(name="status", cls=StatusCommand)
@click.argument("selectors", nargs=-1)
@click.option("-s", "--status", "statuses", multiple=True, type=_StatusChoice(STATES))
@click.option("--static", "static_report", is_flag=True, help="Print one formatted report; --plain takes precedence.")
@click.option("--json", "json_report", is_flag=True, help="Print one machine-readable JSON status report.")
@pass_context
def status(ctx, selectors, statuses, details, plain, static_report, json_report):
    """Show a grouped overview or Task rows selected by names and quoted globs.

    Group labels summarize Tasks; selectors match Task names. Use '*' to list
    Task rows. A capable terminal opens a cached keyboard browser (minimum
    40 x 12). Use --static for one report or --plain for undecorated output.
    Redirected output is plain; --details expands jobs. Press ? for keys and
    q to quit the browser. Inspect again to obtain newer observations.
    """
    if json_report and (plain or static_report):
        raise click.UsageError("--json cannot be combined with --plain or --static.")
    if not (json_report or plain or static_report) and supports_decoration(sys.stdout) and supports_keyboard(sys.stdin):
        width, height = os.get_terminal_size(sys.stdout.fileno())
        if width >= 40 and height >= 12:
            from .browser import run_browser
            raise click.exceptions.Exit(run_browser(ctx, selectors, statuses, details))
        click.echo("Terminal too small for interactive status (minimum 40 × 12); using plain static output.", err=True)
        plain = True
        click.get_current_context().params["plain"] = True
    workflow = load_workflow(ctx)
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        names = status_selection(workflow, selectors)
        plan = plan_workflow(workflow, ctx, selectors=names)
        report = Report(plan.store, plain=plain)
        selected = selected_rows(workflow, plan, names=names)
        rows = [row for row in selected if not statuses or row.state in statuses]
        if json_report:
            return status_document(rows, task_order(workflow, plan))
        total = len(workflow._task_declarations)
        if not selectors and not details:
            report.overview(task_groups(workflow, plan), selected, total,
                            matching={row.task.name for row in rows} if statuses else None)
            report.attention(rows, inspection_command("status", "*"))
            report.status_line("\nGroups summarize Tasks; selectors match Task names.")
            click.echo("  " + shlex.join(inspection_command("status", "*", "--static")), color=False)
        else:
            report.status(rows, total, expand=details)
        report.workflow_notices(task_order(workflow, plan), rows)
