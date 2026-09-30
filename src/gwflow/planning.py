"""Shared, non-persisting workflow planning using the pinned gwf scheduler."""

from copy import copy
from dataclasses import dataclass, field

from gwf import Target
from gwf.backends import BackendStatus, create_backend
from gwf.core import (
    CachedFilesystem,
    Graph,
    NoopSpecHashes,
    UnresolvedInputError,
    get_spec_hashes,
    hash_spec,
)
from gwf.scheduling import SUBMITTED_STATES, schedule, should_run

from ._state import state_name
from .completion import Completion


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


@dataclass
class _Plan:
    """Private result of ordinary gwf scheduling, before submission writes."""

    targets: list
    status_targets: list
    tasks: dict = field(default_factory=dict)
    reused: set = field(default_factory=set)
    submissions: set = field(default_factory=set)
    reasons: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)
    target_reasons: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    upstream_chains: dict = field(default_factory=dict)
    recovery: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _UpstreamPath:
    parent: str | None
    description: str
    ordinary_tail: str = ""


def _target_reason(target, graph, states, fs, hashes, status, *, force=False):
    if force:
        return "forced whole-workflow run"
    if status in (BackendStatus.SUBMITTED, BackendStatus.RUNNING):
        return "already active; ordinary run leaves it alone"
    if status in (BackendStatus.FAILED, BackendStatus.CANCELLED):
        return f"backend reports {state_name(status)} work; retry"
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


def _upstream_chains(tasks, producers, upstream, owners, planned, pending, submissions):
    """Trace the scheduling causes across Task boundaries in the final plan."""
    ordinary = {}

    def treatment(name):
        return "planned upstream work" if name in submissions else "active upstream work"

    def ordinary_paths(name):
        if name not in ordinary:
            label = f"Ordinary target {name}"
            paths = {_UpstreamPath(None, label, label)}
            for dependency in planned.dependencies[planned.targets[name]]:
                if dependency.name not in pending:
                    continue
                parent = owners.get(dependency.name)
                if parent:
                    paths.add(_UpstreamPath(
                        parent, f"Task {parent} target {dependency.name} -> {label}", label,
                    ))
                else:
                    paths.update(_UpstreamPath(
                        path.parent, f"{path.description} -> {label}",
                        f"{path.ordinary_tail} -> {label}",
                    ) for path in ordinary_paths(dependency.name))
            ordinary[name] = sorted(paths, key=lambda path: path.description)
        return ordinary[name]

    direct = {}
    for name in tasks:
        causes = []
        named_targets = set()
        for target in sorted(producers[name] & pending):
            parent = owners.get(target)
            if parent:
                causes.append(_UpstreamPath(
                    parent, f"Task {parent} target {target} ({treatment(target)})",
                ))
            else:
                causes.extend(_UpstreamPath(
                    path.parent, f"{path.description} ({treatment(target)})",
                    path.ordinary_tail,
                ) for path in ordinary_paths(target))
            named_targets.add(target)
        for parent in sorted(upstream[name]):
            for target in tasks[parent][1]:
                if target.name in pending and target.name not in named_targets:
                    causes.append(_UpstreamPath(
                        parent, f"Task {parent} target {target.name} "
                                f"({treatment(target.name)}; whole-Task ordering)",
                    ))
                    named_targets.add(target.name)
            completion = tasks[parent][2]
            finalizer = f"{parent}__gwflow_complete"
            if completion.replaced:
                causes.append(_UpstreamPath(
                    parent, f"Task {parent} Completion job {finalizer} "
                            "(changed Completion attempt)",
                ))
            elif finalizer in pending:
                causes.append(_UpstreamPath(
                    parent, f"Task {parent} Completion job {finalizer} ({treatment(finalizer)})",
                ))
        direct[name] = causes

    chains = {}

    def trace(name):
        if name not in chains:
            paths = set()
            parents = set()
            for cause in direct[name]:
                paths.add(f"{cause.description} -> Task {name}")
                if cause.parent:
                    extension = f" -> {cause.ordinary_tail}" if cause.ordinary_tail else ""
                    parents.add((cause.parent, extension))
            for parent, extension in parents:
                paths.update(f"{path}{extension} -> Task {name}" for path in trace(parent))
            chains[name] = sorted(paths)
        return chains[name]

    return {name: trace(name) for name in tasks}


def plan_workflow(workflow, targets, ctx, *, force=False, details=False):
    """Plan while the caller holds the submission guard; never persist attempts."""
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
                reasons[name] = "backend reports active, failed, or canceled work"
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

        recovery: dict[str, list[str]] = {name: [] for name in tasks}
        for name, (_, inner, completion) in tasks.items():
            finalizer = f"{name}__gwflow_complete"
            for target in [*inner, completion.target(inner, ctx.working_dir)]:
                state = status(target)
                if details and state not in (BackendStatus.COMPLETED, BackendStatus.UNKNOWN):
                    evidence[name].append(f"target {target.name}: backend {state_name(state)} prevents Reuse")
                if (state not in (BackendStatus.SUBMITTED, BackendStatus.RUNNING)
                        or target.name in submissions):
                    continue
                required = "is needed"
                if target.name == finalizer and completion.replaced:
                    cause = f"active Completion job {finalizer} belongs to an earlier attempt"
                elif hashes.has_changed(target) is not None:
                    cause = f"active target {target.name} has a changed or unrecorded command"
                elif target.name == finalizer and completion.commands is None and not completion.is_complete():
                    # Once a replacement is persisted, disabled command tracking
                    # cannot identify whether the active job owns that attempt.
                    cause = f"active Completion job {finalizer} has no matching Completion record yet"
                    required = "may be needed"
                else:
                    continue
                recovery[name].append(
                    f"{cause} and is left alone; "
                    f"after active jobs settle, a later ordinary invocation of `gwf run` {required} "
                    "to recover the current work and Completion evidence"
                )
        target_reasons = {
            target.name: _target_reason(target, planned, states, fs, hashes, status(target), force=force)
            for target in graph.targets.values() if target.name not in owners
        }

    # Validate generated paths as well, before any caller can persist attempts.
    Graph.from_targets(execution, CachedFilesystem())
    upstream_chains = (
        _upstream_chains(tasks, producers, upstream, owners, planned, pending, submissions)
        if details else {}
    )
    # Status retains authored targets for incomplete Tasks and represents
    # reusable Tasks by their existing Completion job. Derive this projection
    # from the same final reuse decisions used by run and explain.
    reused = set(tasks) - expanded
    omitted = {target.name for name in reused for target in tasks[name][1]}
    projected = {target.name: target for target in targets if target.name not in omitted}
    for name in reused:
        finalizer = tasks[name][2].target(tasks[name][1], ctx.working_dir)
        finalizer.inputs = []
        projected[finalizer.name] = finalizer
    return _Plan(
        targets=list(execution.values()), status_targets=list(projected.values()),
        tasks=tasks, reused=reused,
        submissions=submissions, reasons=reasons, observed=observed,
        target_reasons=target_reasons, evidence=evidence,
        upstream_chains=upstream_chains,
        recovery=recovery,
    )
