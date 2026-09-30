"""The controlled persistence and submission boundary for gwflow workflows."""

import click
from gwf import Workflow as GwfWorkflow
from gwf.backends import create_backend
from gwf.core import CachedFilesystem, Graph, get_spec_hashes, pass_context
from gwf.exceptions import WorkflowError
from gwf.filtering import GroupFilter, NameFilter, filter_generic
from gwf.plugins.run import clean_logs, run as gwf_run
from gwf.scheduling import submit_workflow
from .submission import submit_plan

from ._frontend import _submission_guard
from .planning import plan_workflow
from .workflow import Workflow


def _submit_graph(graph, ctx, fs, *, dry_run, force,
                  targets=(), group=(), no_deps=False):
    """Submit through gwf's scheduler and persist its tracking on context exit."""
    if ctx.config.get("clean_logs") and not dry_run:
        clean_logs(ctx.working_dir, graph)
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        with get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config) as hashes:
            filters = []
            if targets:
                filters.append(NameFilter(patterns=targets))
            if group:
                filters.append(GroupFilter(patterns=group))
            endpoints = set(filter_generic(targets=graph, filters=filters))
            # Retain gwf's scheduling and its reobservation of active jobs.
            # A job may settle after planning, while no other gwflow frontend
            # can submit until both tracking contexts have closed.
            submit_workflow(endpoints, graph, fs, hashes, backend,
                            dry_run=dry_run, force=force, no_deps=no_deps)


@pass_context
def _run(ctx, targets, dry_run, force, no_deps, group, force_task=()):
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        if force_task:
            raise WorkflowError("--force-task requires a gwflow.Workflow")
        fs = CachedFilesystem()
        graph = Graph.from_targets(workflow.targets, fs)
        return _submit_graph(graph, ctx, fs, targets=targets, dry_run=dry_run,
                             force=force, no_deps=no_deps, group=group)
    if targets or group or no_deps:
        raise WorkflowError(
            "gwflow supports whole-workflow run only; "
            "selectors and --no-deps are unsupported"
        )
    with _submission_guard(ctx.working_dir):
        plan = plan_workflow(workflow, ctx, force=force, force_tasks=force_task)
        submit_plan(plan, workflow, ctx, dry_run=dry_run)


# Patch the command object too, regardless of plugin discovery order.
if not any(parameter.name == "force_task" for parameter in gwf_run.params):
    gwf_run.params.append(click.Option(["--force-task"], multiple=True, help="Start a fresh attempt for a named whole Task."))
gwf_run.callback = _run
run = gwf_run
