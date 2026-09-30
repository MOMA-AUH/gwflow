"""The controlled persistence and submission boundary for gwflow workflows."""

from gwf import Workflow as GwfWorkflow
from gwf.backends import create_backend
from gwf.core import CachedFilesystem, Graph, get_spec_hashes, pass_context
from gwf.exceptions import WorkflowError
from gwf.filtering import GroupFilter, NameFilter, filter_generic
from gwf.plugins.run import clean_logs, run as gwf_run
from gwf.scheduling import submit_workflow

from ._frontend import _submission_guard
from .planning import plan_workflow
from .workflow import Workflow


def _submit_graph(graph, ctx, fs, *, dry_run, force, preserve_logs=False,
                  targets=(), group=(), no_deps=False):
    """Submit through gwf's scheduler and persist its tracking on context exit."""
    if ctx.config.get("clean_logs") and not dry_run and not preserve_logs:
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


def _submit_plan(plan, ctx, *, dry_run, force):
    """Apply a validated plan while the caller holds the frontend guard."""
    fs = CachedFilesystem()
    graph = Graph.from_targets({target.name: target for target in plan.targets}, fs)
    if not dry_run:
        for name, (_, _, completion) in plan.tasks.items():
            if name not in plan.reused:
                completion.persist()
    # Omitted targets still own useful logs, despite leaving the run graph.
    _submit_graph(graph, ctx, fs, dry_run=dry_run, force=force,
                  preserve_logs=bool(plan.reused))


@pass_context
def _run(ctx, targets, dry_run, force, no_deps, group):
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
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
        declarations = list(workflow.targets.values())
        plan = plan_workflow(workflow, declarations, ctx, force=force)
        _submit_plan(plan, ctx, dry_run=dry_run, force=force)


# Patch the command object too, regardless of plugin discovery order.
gwf_run.callback = _run
run = gwf_run
