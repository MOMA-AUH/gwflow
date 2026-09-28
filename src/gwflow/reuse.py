"""Materialize task reuse for the pinned gwf 2.1.1 CLI.

The target collection is read during Graph construction, after Click has
selected the backend and command options. Planning uses gwf's own scheduler;
its backend context closes before the CLI opens the submitting context.
"""

from contextlib import contextmanager
from copy import copy
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
    if cli is None or not isinstance(cli.obj, Context) or cli.info_name != "run":
        return targets
    if not workflow._task_declarations:
        return targets
    if cli.params.get("targets") or cli.params.get("group") or cli.params.get("no_deps"):
        raise WorkflowError(
            "gwflow supports whole-workflow run only; "
            "selectors and --no-deps are unsupported"
        )

    ctx = cli.obj
    # Click closes the run context after gwf has closed its submitting backend
    # and hash contexts, including on exceptions. Acquire before reading any
    # expected attempts or backend tracking. Repeated materialization in the
    # same command must not try to acquire a second lock on the same file.
    guards = cli.meta.setdefault("gwflow_submission_guards", set())
    if ctx.working_dir not in guards:
        cli.with_resource(_submission_guard(ctx.working_dir))
        guards.add(ctx.working_dir)
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
        completion = Completion(ctx.working_dir, name, definition, commands)
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
    with create_backend(
        ctx.backend, working_dir=ctx.working_dir, config=ctx.config
    ) as backend:
        # Do not close (and thus write) this read-only hash view. The CLI owns
        # saving hashes for actual submissions, including the finalizer.
        hashes = get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config)
        for name, (boundary, inner, completion) in tasks.items():
            if cli.params.get("force") or not completion.is_complete():
                continue
            if any(not fs.exists(path) for path in boundary.flattened_inputs()):
                continue
            if should_run(boundary, fs, NoopSpecHashes()):
                continue
            finalizer = completion.target(inner, ctx.working_dir)
            if any(
                backend.status(target) not in (
                    BackendStatus.COMPLETED, BackendStatus.UNKNOWN
                )
                for target in [*inner, finalizer]
            ):
                continue
            if any(hashes.has_changed(target) is not None for target in inner):
                continue
            reusable.add(name)

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
                planned.endpoints(), planned, fs, hashes, backend.status,
                lambda target, dependencies: submissions.add(target.name),
                force=cli.params.get("force", False),
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
            expanded.update(affected)

    # Validate generated paths as well, before changing expected attempts.
    Graph.from_targets(execution, CachedFilesystem())
    if not cli.params.get("dry_run"):
        for name, (_, _, completion) in tasks.items():
            if name in expanded:
                completion.persist()
    if set(tasks) - expanded:
        # gwf removes logs absent from its execution graph. Disable that cleanup
        # for this invocation only; never dump the changed config to disk.
        ctx.config["clean_logs"] = "false"
    return list(execution.values())
