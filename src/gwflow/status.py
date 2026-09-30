"""Compact Task status through the ordinary gwf CLI."""

from collections import Counter
from copy import copy

import click

from gwf import Workflow as GwfWorkflow
from gwf.backends import create_backend
from gwf.core import CachedFilesystem, Graph, Status, get_spec_hashes, pass_context
from gwf.filtering import EndpointFilter, GroupFilter, NameFilter, StatusFilter, filter_generic
from gwf.plugins.status import status as gwf_status
from gwf.scheduling import get_status_map

from ._state import state_name
from .workflow import Workflow, _bookkeeping_name


# gwf 2.1.1 registers its own `status` entry point after external plugins in
# some environments. Patch that command object as well: Click keeps the object
# by reference, so this works regardless of which entry point is loaded first.
# Keep a copy of the original command for the existing non-tree formats.
_original_status = copy(gwf_status)


class _StatusChoice(click.Choice):
    def convert(self, value, param, ctx):
        # Accept gwf's spelling without advertising it in choices or completion.
        if value == "cancelled":
            value = "canceled"
        return super().convert(value, param, ctx)


_VISUALS = {
    Status.SHOULDRUN: (".", "magenta"),
    Status.SUBMITTED: ("~", "cyan"),
    Status.RUNNING: ("~", "blue"),
    Status.COMPLETED: ("+", "green"),
    Status.FAILED: ("!", "red"),
    Status.CANCELLED: ("!", "red"),
}


def _task_state(states):
    for state in (Status.FAILED, Status.CANCELLED, Status.RUNNING,
                  Status.SUBMITTED, Status.SHOULDRUN):
        if state in states:
            return state
    return Status.COMPLETED


def _task_summary(states, inner_names, reused):
    if reused:
        count = len(inner_names)
        return f"{count} target{'s' if count != 1 else ''} omitted by reuse"
    counts = Counter(states[name] for name in inner_names)
    problems = []
    for state in (Status.FAILED, Status.CANCELLED):
        count = counts[state]
        if count:
            problems.append(f"{count} {state_name(state)} target{'s' if count != 1 else ''}")
    if problems:
        return ", ".join(problems)
    count = len(inner_names)
    return f"{counts[Status.COMPLETED]}/{count} target{'s' if count != 1 else ''} completed"


def _echo_row(symbol, label, state, detail="", *, indent="", color=None):
    line = f"{indent}{symbol} {label:<28} {state:<12} {detail}".rstrip()
    click.secho(line, fg=color)


def _print_tree(workflow, target_states, selected, backend, *, details, filtered):
    states = {target.name: state for target, state in target_states.items()}
    targets_by_name = {target.name: target for target in target_states}
    visible = {target.name for target in selected}
    declared = dict(workflow.targets)
    entries = []
    owned = set()

    for name, (_, _, inner_names, _) in workflow._task_declarations.items():
        completion_name = _bookkeeping_name(name)
        names = (*inner_names, completion_name)
        owned.update(names)
        if not any(target in visible for target in names):
            continue
        order = min((declared[target].order for target in inner_names), default=float("inf"))
        entries.append((order, "task", name, inner_names, completion_name))

    for target in target_states:
        if target.name not in owned and target.name in visible:
            entries.append((target.order, "target", target))

    for entry in sorted(entries, key=lambda item: item[0]):
        if entry[1] == "target":
            target = entry[2]
            state = states[target.name]
            symbol, color = _VISUALS[state]
            tracked_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
            _echo_row(symbol, f"Target {target.name}", state_name(state),
                      f"(id: {tracked_id or 'none'})", color=color)
            continue

        _, _, name, inner_names, completion_name = entry
        present = [target for target in inner_names if target in states]
        reused = not present and completion_name in states
        task_states = [states[target] for target in (*inner_names, completion_name)
                       if target in states]
        task_state = _task_state(task_states)
        symbol, color = _VISUALS[task_state]
        label = "reusable" if reused else state_name(task_state)
        _echo_row(symbol, f"Task {name}", label,
                  _task_summary(states, inner_names, reused), color=color)

        expand = details or task_state in (
            Status.SUBMITTED, Status.RUNNING, Status.FAILED, Status.CANCELLED
        )
        if not expand:
            continue
        children = [target for target in inner_names if target in visible or
                    (details and not filtered and target not in states and
                     completion_name in visible)]
        if details and completion_name in visible:
            children.append(completion_name)
        for index, target_name in enumerate(children):
            last = index == len(children) - 1
            prefix = "  `-- " if last else "  |-- "
            local_name = ("completion" if target_name == completion_name else
                          target_name.removeprefix(f"{name}__"))
            if target_name not in states:
                _echo_row("+", local_name, "omitted by reuse", indent=prefix, color="green")
                continue
            state = states[target_name]
            symbol, child_color = _VISUALS[state]
            target = targets_by_name[target_name]
            tracked_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
            _echo_row(symbol, local_name, state_name(state),
                      f"(id: {tracked_id or 'none'})", indent=prefix, color=child_color)


@click.command(name="status")
@click.argument("targets", nargs=-1)
@click.option("--endpoints", is_flag=True, default=False, help="Show only endpoints.")
@click.option("-f", "--format", "output_format", default="tree",
              type=click.Choice(("tree", "default", "summary", "grouped")),
              help="How to format status output.")
@click.option("-s", "--status", "statuses", multiple=True,
              type=_StatusChoice(tuple(state_name(state) for state in Status)),
              help="Filter by state.")
@click.option("-g", "--group", multiple=True)
@click.option("--details", is_flag=True, help="Expand visible Task targets and Completion jobs in tree view.")
@pass_context
def tree_status(ctx, targets, endpoints, output_format, statuses, group, details):
    """Show target status, grouped under Tasks in gwflow workflows."""
    statuses = tuple("cancelled" if name == "canceled" else name for name in statuses)
    if output_format != "tree":
        return click.get_current_context().invoke(
            _original_status, targets=targets, endpoints=endpoints,
            format=output_format, status=statuses, group=group)

    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        return click.get_current_context().invoke(
            _original_status, targets=targets, endpoints=endpoints,
            format="default", status=statuses, group=group)

    fs = CachedFilesystem()
    graph = Graph.from_targets(workflow.targets, fs)
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        with get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config) as hashes:
            states = get_status_map(graph, fs, hashes, backend)
            filters = []
            if statuses:
                filters.append(StatusFilter(states.get, [Status[name.upper()] for name in statuses]))
            if targets:
                filters.append(NameFilter(targets))
            if endpoints:
                filters.append(EndpointFilter(graph.endpoints()))
            if group:
                filters.append(GroupFilter(group))
            selected = set(filter_generic(graph, filters))
            _print_tree(workflow, states, selected, backend, details=details,
                        filtered=bool(statuses or targets or endpoints or group))


gwf_status.params = tree_status.params
gwf_status.callback = tree_status.callback
gwf_status.help = tree_status.help
status = gwf_status
