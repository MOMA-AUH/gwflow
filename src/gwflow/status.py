"""Compact managed Task status and unchanged gwf formatting for plain workflows."""

import click
from gwf import Workflow as GwfWorkflow
from gwf.backends import create_backend
from gwf.core import CachedFilesystem, Graph, Status, get_spec_hashes, pass_context
from gwf.filtering import EndpointFilter, GroupFilter, NameFilter, StatusFilter, filter_generic
from gwf.plugins.status import FORMATS, status as gwf_status
from gwf.scheduling import get_status_map

from ._frontend import _submission_guard
from ._state import state_name
from .inspection import blockage_line
from .planning import plan_workflow
from .presentation import STATES, Report, output_options, selected_rows, task_groups
from .selection import status_selection
from .workflow import Workflow


class _StatusChoice(click.Choice):
    def convert(self, value, param, ctx):
        return super().convert("canceled" if value == "cancelled" else value, param, ctx)


def _plain_status(workflow, ctx, targets, endpoints, output_format, statuses, group):
    if set(statuses) - {state_name(state) for state in Status}:
        raise click.UsageError("Ordinary gwf status filters use target states: "
                               + ", ".join(state_name(state) for state in Status))
    fs = CachedFilesystem()
    graph = Graph.from_targets(workflow.targets, fs)
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        with get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config) as hashes:
            states = get_status_map(graph, fs, hashes, backend)
            filters = []
            if statuses:
                filters.append(StatusFilter(states.get, [Status[("cancelled" if name == "canceled" else name).upper()]
                                                         for name in statuses]))
            if targets:
                filters.append(NameFilter(targets))
            if endpoints:
                filters.append(EndpointFilter(graph.endpoints()))
            if group:
                filters.append(GroupFilter(group))
            selected = set(filter_generic(graph, filters))
            FORMATS["default" if output_format == "tree" else output_format](
                {target: state for target, state in states.items() if target in selected}, backend)


@output_options
@click.command(name="status")
@click.argument("targets", nargs=-1)
@click.option("--endpoints", is_flag=True, default=False,
              help="Show endpoint Tasks (endpoint targets in plain gwf workflows).")
@click.option("-f", "--format", "output_format", default="tree", type=click.Choice(("tree", "default", "summary", "grouped")),
              help="Format for ordinary gwf workflows only.")
@click.option("-s", "--status", "statuses", multiple=True, type=_StatusChoice(tuple(dict.fromkeys((*STATES, *(state_name(state) for state in Status))))))
@click.option("-g", "--group", multiple=True)
@click.option("--instances", is_flag=True, help="Show every selected Task with its exact state and completed-job progress.")
@click.option("--details", is_flag=True,
              help="Expand Tasks with nested targets and lifecycle jobs. Use gwf explain --details for diagnostics.")
@pass_context
def managed_status(ctx, targets, endpoints, output_format, statuses, group, instances, details, plain, no_truncate):
    """Show grouped managed Task condition, including reusable cleaned work."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        return _plain_status(workflow, ctx, targets, endpoints, output_format, statuses, group)
    if output_format != "tree":
        raise click.UsageError("--format is only supported for ordinary gwf workflows; use --details for managed Tasks")
    invalid = set(statuses) - set(STATES)
    if invalid:
        raise click.UsageError("Managed status filters use Task states: " + ", ".join(STATES))
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        plan = plan_workflow(workflow, ctx)
        groups = task_groups(workflow, plan)
        names, view = status_selection(plan, groups, targets)
        if details:
            view = "details"
        elif instances:
            view = "instances"
        report = Report(plan.store, plain=plain, no_truncate=no_truncate)
        if plan.blocked:
            report.notice(blockage_line(plan))
        rows = [row for row in selected_rows(workflow, plan, endpoints=endpoints, statuses=statuses, group=group)
                if row.task.name in names]
        if view == "overview":
            report.overview(groups, rows, len(plan.tasks))
        else:
            report.status(rows, len(plan.tasks), expand=view == "details")


gwf_status.params = managed_status.params
gwf_status.callback = managed_status.callback
gwf_status.help = managed_status.help
status = gwf_status
