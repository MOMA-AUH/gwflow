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
from .planning import plan_workflow
from .workflow import Workflow, lifecycle_jobs


class _StatusChoice(click.Choice):
    def convert(self, value, param, ctx):
        return super().convert("canceled" if value == "cancelled" else value, param, ctx)


def _plain_status(workflow, ctx, targets, endpoints, output_format, statuses, group):
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
@click.option("--endpoints", is_flag=True, default=False)
@click.option("-f", "--format", "output_format", default="tree", type=click.Choice(("tree", "default", "summary", "grouped")))
@click.option("-s", "--status", "statuses", multiple=True, type=_StatusChoice(tuple(state_name(state) for state in Status)))
@click.option("-g", "--group", multiple=True)
@click.option("--details", is_flag=True, help="Show public target names and execution jobs.")
@pass_context
def managed_status(ctx, targets, endpoints, output_format, statuses, group, details):
    """Show managed Task condition, including reusable cleaned work."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        return _plain_status(workflow, ctx, targets, endpoints, output_format, statuses, group)
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        plan = plan_workflow(workflow, ctx)
        for task in plan.tasks:
            public = [f"{task.name}__{local}" for local in lifecycle_jobs(task.structure["targets"])]
            if targets and not any(fnmatchcase(name, pattern) for name in [task.name, *public] for pattern in targets):
                continue
            if group and not any((target.group or "none") in group
                                 for target in workflow._task_declarations[task.name].targets.values()):
                continue
            state = {"retry": "shouldrun", "continue": "shouldrun", "prepare": "shouldrun", "fresh": "shouldrun", "reuse": "completed", "active": "running", "blocked": "failed"}[task.action]
            if statuses and state not in statuses:
                continue
            label = ("reusable work-present" if task.work_present else "reusable work-cleaned") if task.action == "reuse" else task.action
            click.echo(f"Task {task.name}: {label}; {task.reason}")
            if details:
                for local in lifecycle_jobs(task.structure["targets"]):
                    item = task.submissions.get(local)
                    job = item.submission if item and item.intent else "not submitted"
                    click.echo(f"  {task.name}__{local}: {job}")
                if task.attempt:
                    click.echo(f"  Attempt: {task.attempt['attempt']}; workspace: {plan.store.workspace(task.attempt)}")
                    for name, expected in task.attempt["producers"].items():
                        click.echo(f"  Expected producer {name}: {expected}")
                    for name, jobs in task.consumers.items():
                        click.echo(f"  Active consumer {name}: " + ", ".join(f"{item.submission} ({item.state})" for item in jobs))


gwf_status.params = managed_status.params
gwf_status.callback = managed_status.callback
gwf_status.help = managed_status.help
status = gwf_status
