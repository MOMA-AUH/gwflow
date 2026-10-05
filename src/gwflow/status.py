"""Compact managed Task status and unchanged gwf formatting for plain workflows."""

from fnmatch import fnmatchcase

import click
from gwf import Workflow as GwfWorkflow
from gwf.backends import create_backend
from gwf.core import CachedFilesystem, Graph, Status, get_spec_hashes, pass_context
from gwf.filtering import EndpointFilter, GroupFilter, NameFilter, StatusFilter, filter_generic
from gwf.plugins.status import FORMATS, status as gwf_status
from gwf.scheduling import get_status_map

from ._frontend import _submission_guard
from ._state import state_name
from .inspection import blockage_line, condition, task_details
from .lifecycle import ordered_targets, producer_names
from .planning import plan_workflow
from .presentation import STATES, print_status, task_order, task_row
from .workflow import Workflow, lifecycle_jobs


class _StatusChoice(click.Choice):
    def convert(self, value, param, ctx):
        return super().convert("canceled" if value == "cancelled" else value, param, ctx)


def _matches(name, patterns):
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _managed_rows(workflow, plan, targets, endpoints, statuses, group):
    producers = {name for task in plan.tasks for name in producer_names(task.structure)}
    for task in task_order(workflow, plan):
        if endpoints and task.name in producers:
            continue
        row = task_row(task)
        names = lifecycle_jobs(ordered_targets(task.structure))
        if targets and not (_matches(task.name, targets)
                            or any(_matches(f"{task.name}__{local}", targets) for local in names)):
            continue
        if group and not any(_matches(workflow.targets[f"{task.name}__{local}"].group or "none", group)
                             for local in task.structure["targets"]):
            continue
        if statuses and row.state not in statuses:
            continue
        yield row


def _print_details(plan, rows, *, details, focused, targets):
    for row in rows:
        task = row.task
        if details or focused:
            names = lifecycle_jobs(ordered_targets(task.structure))
            children = [local for local in names if local in task.structure["targets"] or details
                        or _matches(f"{task.name}__{local}", targets)]
            for index, local in enumerate(children):
                name = {"gwflow_prepare": "preparation", "gwflow_complete": "completion"}.get(local, local)
                state = row.jobs[local]
                prefix = "  `-- " if index == len(children) - 1 else "  |-- "
                click.echo(f"{prefix}. {name:<28} {state}")
        if details:
            click.echo(f"  Condition: {condition(task)}")
            click.echo(f"  Next: {task.action}")
            click.echo(f"  Reason: {task.reason}")
            for line in task_details(plan.store, task):
                click.echo(f"  {line}")


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


@click.command(name="status")
@click.argument("targets", nargs=-1)
@click.option("--endpoints", is_flag=True, default=False,
              help="Show endpoint Tasks (endpoint targets in plain gwf workflows).")
@click.option("-f", "--format", "output_format", default="tree", type=click.Choice(("tree", "default", "summary", "grouped")),
              help="Format for ordinary gwf workflows only.")
@click.option("-s", "--status", "statuses", multiple=True, type=_StatusChoice(tuple(dict.fromkeys((*STATES, *(state_name(state) for state in Status))))))
@click.option("-g", "--group", multiple=True)
@click.option("--details", is_flag=True, help="Expand the tree and include lifecycle diagnostics and execution jobs.")
@pass_context
def managed_status(ctx, targets, endpoints, output_format, statuses, group, details):
    """Show managed Task condition, including reusable cleaned work."""
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
        if plan.blocked:
            click.secho(blockage_line(plan), fg="yellow")
        rows = list(_managed_rows(workflow, plan, targets, endpoints, statuses, group))
        print_status(rows, len(plan.tasks))
        _print_details(plan, rows, details=details, focused=bool(targets or group), targets=targets)


gwf_status.params = managed_status.params
gwf_status.callback = managed_status.callback
gwf_status.help = managed_status.help
status = gwf_status
