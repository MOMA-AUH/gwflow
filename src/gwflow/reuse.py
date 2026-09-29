"""Materialize task reuse for the pinned gwf 2.1.1 CLI.

The target collection is read during Graph construction, after Click has
selected the backend and command options. Planning uses gwf's own scheduler;
its backend context closes before the CLI opens the submitting context.
"""

from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, field
import fcntl
from pathlib import Path

import click

from gwf import Target
from gwf.backends import BackendStatus, create_backend
from gwf.core import (
    CachedFilesystem,
    Context,
    Graph,
    NoopSpecHashes,
    UnresolvedInputError,
    get_spec_hashes,
    hash_spec,
)
from gwf.exceptions import WorkflowError
from gwf.scheduling import SUBMITTED_STATES, schedule, should_run

from .completion import Completion


@contextmanager
def _submission_guard(working_dir):
    # gwf saves command hashes and backend tracking on context exit. Keep
    # concurrent runs out until those writes finish, not just until planning
    # or submission returns. The OS releases this lock on CLI interruption;
    # jobs neither inherit nor wait for it.
    path = Path(working_dir) / ".gwf" / "gwflow-submission.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _execution_targets(graph, tasks, upstream, expanded, working_dir):
    """Add completion dependencies without changing authored declarations."""
    owned = {target.name for _, inner, _ in tasks.values() for target in inner}
    targets = {
        name: target for name, target in graph.targets.items() if name not in owned
    }
    for name in expanded:
        boundary, inner, completion = tasks[name]
        dependencies = sorted(str(tasks[parent][2].path) for parent in upstream[name])
        for target in inner:
            materialized = copy(target)
            materialized.inputs = [*target.flattened_inputs(), *dependencies]
            targets[target.name] = materialized
        finalizer = completion.target(inner, working_dir)
        finalizer.inputs = sorted({
            *finalizer.inputs, *boundary.flattened_inputs(), *dependencies,
        })
        targets[finalizer.name] = finalizer
    return targets


def materialize(workflow, targets):
    cli = click.get_current_context(silent=True)
    if cli is None or not isinstance(cli.obj, Context) or cli.info_name not in ("run", "status"):
        return targets
    inspecting = cli.info_name == "status"
    if not inspecting and (cli.params.get("targets") or cli.params.get("group") or cli.params.get("no_deps")):
        raise WorkflowError(
            "gwflow supports whole-workflow run only; "
            "selectors and --no-deps are unsupported"
        )
    if not workflow._task_declarations:
        return targets

    ctx = cli.obj
    if not inspecting:
        # Click closes the run context after gwf has closed its submitting
        # backend and hash contexts. Acquire before reading expected attempts
        # or backend tracking, including for repeated materialization.
        guards = cli.meta.setdefault("gwflow_submission_guards", set())
        if ctx.working_dir not in guards:
            cli.with_resource(_submission_guard(ctx.working_dir))
            guards.add(ctx.working_dir)
    plan = plan_workflow(workflow, targets, ctx, force=cli.params.get("force", False),
                         status_projection=inspecting)
    if not inspecting:
        if not cli.params.get("dry_run"):
            for name, (_, _, completion) in plan.tasks.items():
                if name not in plan.reused:
                    completion.persist()
        if plan.reused:
            # gwf removes logs absent from its execution graph. Keep omitted
            # targets' logs without dumping this temporary config to disk.
            ctx.config["clean_logs"] = "false"
    return plan.targets


@dataclass
class _Plan:
    """Private result of ordinary gwf scheduling, before submission writes."""

    targets: list
    tasks: dict = field(default_factory=dict)
    reused: set = field(default_factory=set)
    submissions: set = field(default_factory=set)
    reasons: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)
    target_reasons: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


def _target_reason(target, graph, states, fs, hashes, status):
    if status in (BackendStatus.SUBMITTED, BackendStatus.RUNNING):
        return "already active; ordinary run leaves it alone"
    if status in (BackendStatus.FAILED, BackendStatus.CANCELLED):
        return f"backend reports {status.name.lower()} work; retry"
    if any(states[dep] in SUBMITTED_STATES for dep in graph.dependencies[target]):
        return "dependency work requires submission"
    if hashes.has_changed(target) is not None:
        return "tracked target command changed or has no saved hash"
    if any(not fs.exists(path) for path in target.flattened_outputs()):
        return "output is missing"
    if not target.outputs:
        return "outputless target runs on every invocation"
    if should_run(target, fs, hashes):
        return "files are not up to date"
    return "files and tracked command are up to date"


def _reuse_evidence(boundary, inner, completion, fs, hashes):
    reasons = completion.evidence()
    boundary_reasons = []
    inputs = sorted(set(boundary.flattened_inputs()))
    outputs = sorted(set(boundary.flattened_outputs()))
    if not boundary.outputs:
        boundary_reasons.append("Task has no retained outputs; gwf boundary freshness cannot allow Reuse")
    for path in inputs:
        if not fs.exists(path):
            boundary_reasons.append(f"external input is missing and must be produced upstream: {path}")
    for path in outputs:
        if not fs.exists(path):
            boundary_reasons.append(f"retained output is missing: {path}")
        else:
            newer = [source for source in inputs
                     if fs.exists(source) and fs.changed_at(source) > fs.changed_at(path)]
            if newer:
                boundary_reasons.append(f"retained output is stale: {path}; newer external inputs: {', '.join(newer)}")
    reasons.extend(boundary_reasons or ["boundary files are up to date under gwf mtime rules"])
    if completion.commands is None:
        reasons.append("command tracking is disabled; command changes do not prevent Reuse")
    else:
        reasons.append("command tracking is enabled; checking saved Completion and gwf target command hashes")
        for target in inner:
            if hashes.has_changed(target) is not None:
                reasons.append(f"target {target.name}: tracked target command changed or has no saved hash; "
                               "old command text cannot be recovered from a hash")
    return reasons


def plan_workflow(workflow, targets, ctx, *, force=False, status_projection=False, details=False):
    """Plan while the caller holds the submission guard; never persist attempts."""
    inspecting = status_projection
    fs = CachedFilesystem()
    graph = Graph.from_targets(targets, fs)
    tasks = {}
    for name, (inputs, outputs, names, working_dir) in workflow._task_declarations.items():
        # Boundary paths use the same normalization as registered inner targets.
        boundary = Target(
            name=name, inputs=inputs, outputs=outputs,
            options={}, working_dir=working_dir,
        )
        for path in boundary.flattened_inputs():
            if path not in graph.provides and not fs.exists(path):
                raise UnresolvedInputError(
                    f'File "{path}" is required by task "{name}", but does not '
                    "exist and is not provided by any target in the workflow."
                )
        inner = [graph.targets[target_name] for target_name in names]
        definition = {
            "inputs": sorted(set(boundary.flattened_inputs())),
            "outputs": sorted(set(boundary.flattened_outputs())),
            "targets": {
                target.name: {
                    "inputs": sorted(set(target.flattened_inputs())),
                    "outputs": sorted(set(target.flattened_outputs())),
                }
                for target in inner
            },
        }
        commands = (
            {target.name: hash_spec(target.spec) for target in inner}
            if ctx.config.get("use_spec_hashes") else None
        )
        completion = Completion(
            ctx.working_dir, name, definition, commands,
            {**workflow.defaults, **workflow.completion_defaults},
        )
        tasks[name] = (boundary, inner, completion)

    owners = {
        target.name: name
        for name, (_, inner, _) in tasks.items() for target in inner
    }
    producers = {
        name: {
            graph.provides[path].name for path in boundary.flattened_inputs()
            if path in graph.provides
        }
        for name, (boundary, _, _) in tasks.items()
    }
    upstream = {
        name: {
            owners[target] for target in names
            if target in owners and owners[target] != name
        }
        for name, names in producers.items()
    }

    reusable = set()
    reasons = {}
    observed = {}
    evidence = {}
    with create_backend(
        ctx.backend, working_dir=ctx.working_dir, config=ctx.config
    ) as backend:
        # Do not close (and thus write) this read-only hash view. The CLI owns
        # saving hashes for actual submissions, including the finalizer.
        hashes = get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config)

        def status(target):
            if target.name not in observed:
                observed[target.name] = backend.status(target)
            return observed[target.name]

        for name, (boundary, inner, completion) in tasks.items():
            if details:
                evidence[name] = _reuse_evidence(boundary, inner, completion, fs, hashes)
            if force:
                reasons[name] = "forced run"
            elif not completion.is_complete():
                reasons[name] = "completion evidence is missing, invalid, or does not match"
            elif any(not fs.exists(path) for path in boundary.flattened_inputs()):
                reasons[name] = "external input is missing and must be produced upstream"
            elif any(not fs.exists(path) for path in boundary.flattened_outputs()):
                reasons[name] = "retained output is missing"
            elif should_run(boundary, fs, NoopSpecHashes()):
                reasons[name] = "boundary files are not up to date"
            elif any(
                status(target) not in (BackendStatus.COMPLETED, BackendStatus.UNKNOWN)
                for target in [*inner, completion.target(inner, ctx.working_dir)]
            ):
                reasons[name] = "backend reports active, failed, or cancelled work"
            elif any(hashes.has_changed(target) is not None for target in inner):
                reasons[name] = "tracked target command changed or has no saved hash"
            else:
                reusable.add(name)
                reasons[name] = "completion evidence and boundary files allow reuse"

        for _, _, completion in tasks.values():
            completion.prepare()
        # Whole-task edges can introduce cycles even in an acyclic inner file
        # graph. Validate all declarations and generated paths before omission.
        Graph.from_targets(
            _execution_targets(graph, tasks, upstream, set(tasks), ctx.working_dir), fs,
        )

        if inspecting:
            # Keep the submitted finalizer's identity, output and command so
            # gwf can use its backend status and saved spec hash. Its original
            # inputs include removable intermediates and are irrelevant after
            # the task has qualified for reuse.
            omitted = {
                target.name for name in reusable for target in tasks[name][1]
            }
            projected = {
                target.name: target for target in targets if target.name not in omitted
            }
            for name in reusable:
                finalizer = tasks[name][2].target(tasks[name][1], ctx.working_dir)
                finalizer.inputs = []
                projected[finalizer.name] = finalizer
            return _Plan(targets=list(projected.values()))

        expanded = set(tasks) - reusable
        while True:
            replaced = {
                name for name, (_, _, completion) in tasks.items() if completion.replaced
            }
            execution = _execution_targets(
                graph, tasks, upstream, expanded, ctx.working_dir,
            )
            planned = Graph.from_targets(execution, fs)
            submissions = set()
            states = schedule(
                planned.endpoints(), planned, fs, hashes, status,
                lambda target, dependencies: submissions.add(target.name),
                force=force,
            )
            pending = {
                target.name for target, state in states.items() if state in SUBMITTED_STATES
            }
            for name in expanded:
                _, inner, completion = tasks[name]
                completion.prepare(
                    new_work=any(target.name in submissions for target in inner)
                )
            now_replaced = {
                name for name, (_, _, completion) in tasks.items() if completion.replaced
            }
            affected = {
                name for name in set(tasks) - expanded
                if producers[name] & pending or any(
                    parent in now_replaced or f"{parent}__gwflow_complete" in pending
                    for parent in upstream[name]
                )
            }
            if not affected and now_replaced == replaced:
                break
            # Both sets only grow. Completion.prepare allocates at most once,
            # so each pass propagates stable replacement paths further downstream.
            for name in affected:
                reasons[name] = "upstream work or a replaced completion attempt prevents reuse"
            expanded.update(affected)

        for name, (_, inner, completion) in tasks.items():
            for target in [*inner, completion.target(inner, ctx.working_dir)]:
                state = status(target)
                if details and state not in (BackendStatus.COMPLETED, BackendStatus.UNKNOWN):
                    evidence[name].append(f"target {target.name}: backend {state.name.lower()} prevents Reuse")
        target_reasons = {
            target.name: _target_reason(target, planned, states, fs, hashes, status(target))
            for target in graph.targets.values() if target.name not in owners
        }

    # Validate generated paths as well, before any caller can persist attempts.
    Graph.from_targets(execution, CachedFilesystem())
    return _Plan(
        targets=list(execution.values()), tasks=tasks, reused=set(tasks) - expanded,
        submissions=submissions, reasons=reasons, observed=observed,
        target_reasons=target_reasons, evidence=evidence,
    )
