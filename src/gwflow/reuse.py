"""Materialize task reuse for the pinned gwf 2.1.1 CLI.

The target collection is read during Graph construction, after Click has
selected the backend and command options. Planning uses gwf's own scheduler;
its backend context closes before the CLI opens the submitting context.
"""

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
from gwf.scheduling import schedule, should_run

from .completion import Completion


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

        omitted = {target.name for name in reusable for target in tasks[name][1]}
        expanded = {
            name: target for name, target in graph.targets.items()
            if name not in omitted
        }
        for name, (_, inner, completion) in tasks.items():
            if name not in reusable:
                completion.prepare()
                finalizer = completion.target(inner, ctx.working_dir)
                expanded[finalizer.name] = finalizer

        planned = Graph.from_targets(expanded, fs)
        submissions = set()
        schedule(
            planned.endpoints(), planned, fs, hashes, backend.status,
            lambda target, dependencies: submissions.add(target.name),
            force=cli.params.get("force", False),
        )
        for name, (_, inner, completion) in tasks.items():
            if name in reusable:
                continue
            completion.prepare(
                new_work=any(target.name in submissions for target in inner)
            )
            finalizer = completion.target(inner, ctx.working_dir)
            expanded[finalizer.name] = finalizer

    # Validate generated paths as well, before changing expected attempts.
    Graph.from_targets(expanded, CachedFilesystem())
    if not cli.params.get("dry_run"):
        for name, (_, _, completion) in tasks.items():
            if name not in reusable:
                completion.persist()
    if reusable:
        # gwf removes logs absent from its execution graph. Disable that cleanup
        # for this invocation only; never dump the changed config to disk.
        ctx.config["clean_logs"] = "false"
    return list(expanded.values())
