"""Compact managed Task status and unchanged gwf formatting for plain workflows."""

from fnmatch import fnmatchcase

import click
from gwf import Workflow as GwfWorkflow
from gwf.backends import BackendStatus, create_backend
from gwf.core import CachedFilesystem, Graph, Status, get_spec_hashes, pass_context
from gwf.filtering import EndpointFilter, GroupFilter, NameFilter, StatusFilter, filter_generic
from gwf.plugins.status import FORMATS, status as gwf_status
from gwf.scheduling import get_status_map

from ._frontend import _submission_guard
from ._state import state_name
from .inspection import blockage_line, condition, task_details
from .lifecycle import ordered_targets, producer_names
from .planning import plan_workflow
from .workflow import Workflow, lifecycle_jobs


class _StatusChoice(click.Choice):
    def convert(self, value, param, ctx):
        return super().convert("canceled" if value == "cancelled" else value, param, ctx)


_VISUALS = {
    Status.SHOULDRUN: (".", "magenta"),
    Status.SUBMITTED: ("~", "cyan"),
    Status.RUNNING: ("~", "blue"),
    Status.COMPLETED: ("+", "green"),
    Status.FAILED: ("!", "red"),
    Status.CANCELLED: ("!", "red"),
}
_ACTIVE_OR_PROBLEM = (Status.SUBMITTED, Status.RUNNING, Status.FAILED, Status.CANCELLED)


def _job_state(task, local):
    # Reuse remains valid after cleanup; old executions do not describe a fresh run.
    if task.action == "reuse":
        return Status.COMPLETED
    if task.action in ("fresh", "initialize"):
        return Status.SHOULDRUN
    item = task.submissions.get(local)
    if item is None or item.state == "pending":
        return Status.SHOULDRUN
    if item.state == "active":
        return Status.SUBMITTED if item.backend_state == BackendStatus.SUBMITTED else Status.RUNNING
    return {"complete": Status.COMPLETED, "failed": Status.FAILED,
            "cancelled": Status.CANCELLED, "uncertain": Status.FAILED}[item.state]


def _task_row(task, states):
    completed = sum(states[local] == Status.COMPLETED for local in task.structure["targets"])
    count = len(task.structure["targets"])
    progress = f"{completed}/{count} target{'s' if count != 1 else ''} completed"
    if task.action == "reuse":
        detail = progress + "; work-present" if task.work_present else "work-cleaned"
        return Status.COMPLETED, "reusable", detail
    if task.action == "blocked":
        return Status.FAILED, "blocked", "see gwf explain " + task.name
    if task.action == "repair":
        return Status.SHOULDRUN, "repair needed", "retained results damaged"
    if task.action == "deferred":
        return Status.SHOULDRUN, "waiting", "producer results"
    if task.action == "transfer":
        return Status.SHOULDRUN, "results transfer", "recovery needed"
    state = next((state for state in (Status.FAILED, Status.CANCELLED, Status.RUNNING,
                                     Status.SUBMITTED, Status.SHOULDRUN)
                  if state in states.values()), Status.SHOULDRUN)
    if condition(task) == "results transfer (active)":
        return state, "results transfer", state_name(state)
    if task.action == "retry":
        progress += "; retry available"
    elif task.action == "fresh" and task.attempt:
        progress += "; fresh computation required"
    label = "pending" if state == Status.SHOULDRUN else state_name(state)
    return state, label, progress


def _echo_row(name, state, label, detail="", *, indent="", color=None):
    symbol, default_color = _VISUALS[state]
    click.secho(f"{indent}{symbol} {name:<28} {label:<16} {detail}".rstrip(),
                fg=color or default_color)


def _matches(name, patterns):
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _managed_rows(workflow, plan, targets, endpoints, statuses, group):
    producers = {name for task in plan.tasks for name in producer_names(task.structure)}
    for task in plan.tasks:
        if endpoints and task.name in producers:
            continue
        names = lifecycle_jobs(ordered_targets(task.structure))
        states = {local: _job_state(task, local) for local in names}
        row = _task_row(task, states)
        selected = [local for local in names
                    if not targets or _matches(task.name, targets)
                    or _matches(f"{task.name}__{local}", targets)]
        if group:
            selected = [local for local in selected
                        if _matches(workflow.targets[f"{task.name}__{local}"].group or "none", group)]
        if statuses and state_name(row[0]) not in statuses:
            selected = [local for local in selected if state_name(states[local]) in statuses]
        if selected:
            yield task, states, row, selected


def _print_tree(plan, rows, *, details, focused, targets):
    for task, states, (state, label, detail), selected in rows:
        color = "yellow" if label in ("blocked", "repair needed", "waiting") or task.action == "transfer" else None
        _echo_row(f"Task {task.name}", state, label, detail, color=color)
        if details or focused or state in _ACTIVE_OR_PROBLEM or task.action == "transfer":
            children = [local for local in selected if local in task.structure["targets"] or details
                        or _matches(f"{task.name}__{local}", targets)
                        or states[local] in (Status.RUNNING, Status.FAILED, Status.CANCELLED)]
            for index, local in enumerate(children):
                name = {"gwflow_prepare": "preparation", "gwflow_complete": "completion"}.get(local, local)
                child_state = states[local]
                child_label = "pending" if child_state == Status.SHOULDRUN else state_name(child_state)
                item = task.submissions.get(local)
                if item and item.state == "uncertain" and task.action != "reuse":
                    child_label = "blocked"
                prefix = "  `-- " if index == len(children) - 1 else "  |-- "
                _echo_row(name, child_state, child_label, indent=prefix)
        if details:
            click.echo(f"  Condition: {condition(task)}")
            click.echo(f"  Next: {task.action}")
            click.echo(f"  Reason: {task.reason}")
            for line in task_details(plan.store, task):
                click.echo(f"  {line}")


def _print_targets(workflow, rows, output_format, statuses):
    target_states = {}
    for task, states, _, selected in rows:
        for local in selected:
            if local not in task.structure["targets"]:
                continue
            if statuses and state_name(states[local]) not in statuses:
                continue
            target = workflow.targets[f"{task.name}__{local}"]
            target_states[target] = states[local]
            if output_format == "default":
                _echo_row(target.name, states[local], state_name(states[local]))
    # gwf's summary formatter assumes at least one match.
    if output_format != "default" and target_states:
        FORMATS[output_format](target_states, None)


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
@click.option("--endpoints", is_flag=True, default=False,
              help="Show endpoint Tasks (endpoint targets in plain gwf workflows).")
@click.option("-f", "--format", "output_format", default="tree", type=click.Choice(("tree", "default", "summary", "grouped")),
              help="Task tree, flat computation targets, target counts, or counts by group.")
@click.option("-s", "--status", "statuses", multiple=True, type=_StatusChoice(tuple(state_name(state) for state in Status)))
@click.option("-g", "--group", multiple=True)
@click.option("--details", is_flag=True, help="Expand the tree and include lifecycle diagnostics and execution jobs.")
@pass_context
def managed_status(ctx, targets, endpoints, output_format, statuses, group, details):
    """Show managed Task condition, including reusable cleaned work."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        return _plain_status(workflow, ctx, targets, endpoints, output_format, statuses, group)
    if details and output_format != "tree":
        raise click.UsageError("--details requires --format tree for managed workflows")
    with _submission_guard(ctx.working_dir, waiting_message="Waiting for frontend submission bookkeeping..."):
        plan = plan_workflow(workflow, ctx)
        if plan.blocked:
            click.secho(blockage_line(plan), fg="yellow")
        rows = _managed_rows(workflow, plan, targets, endpoints, statuses, group)
        if output_format == "tree":
            _print_tree(plan, rows, details=details, focused=bool(targets or statuses or group), targets=targets)
        else:
            _print_targets(workflow, rows, output_format, statuses)


gwf_status.params = managed_status.params
gwf_status.callback = managed_status.callback
gwf_status.help = managed_status.help
status = gwf_status
