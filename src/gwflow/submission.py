"""Initialize and admit only the jobs selected by a managed lifecycle plan."""

import logging
import os
import shlex
import sys

from gwf import Target
from gwf.backends import create_backend
from gwf.core import get_spec_hashes
from gwf.exceptions import WorkflowError

from . import _files, admission, transfer
from .lifecycle import producer_names

from .workflow import lifecycle_jobs


logger = logging.getLogger(__name__)


def _job(store, attempt, local, workflow, intent):
    finishing = local == "gwflow_complete"
    preparing = local == "gwflow_prepare"
    computing = not (finishing or preparing)
    declaration = workflow._task_declarations[attempt["task"]]
    command = [sys.executable, "-m", "gwflow.execution", str(store.owner_path),
               attempt["task"], attempt["attempt"], "finish" if finishing else "prepare" if preparing else "execute", intent["admission"]]
    if computing:
        command.append(local)
    options = ({**workflow.defaults, **workflow.completion_defaults} if finishing
               else {**workflow.defaults, **workflow.preparation_defaults} if preparing
               else declaration.targets[local].options)
    kwargs = {}
    if computing and declaration.targets[local].executor is not None:
        kwargs["executor"] = declaration.targets[local].executor
    elif workflow.executor is not None:
        kwargs["executor"] = workflow.executor
    return Target(name=intent["submission"], inputs=[], outputs=[], options=dict(options),
                  working_dir=workflow.working_dir, spec=shlex.join(command), **kwargs)


def _log_aliases(working_dir, public_name, job_name):
    # gwf logs continues to accept the public Task/local-target name. Actual
    # execution logs stay under their generation names independently of work.
    with _files.directory(os.path.join(working_dir, ".gwf", "logs"), create=True) as logs:
        for suffix in ("stdout", "stderr"):
            alias = f"{public_name}.{suffix}"
            if os.path.lexists(os.path.join(working_dir, ".gwf", "logs", alias)):
                os.unlink(alias, dir_fd=logs)
            os.symlink(f"{job_name}.{suffix}", alias, dir_fd=logs)
        _files.sync_directory(logs)


def submit_plan(plan, workflow, ctx, *, dry_run):
    blocked = [task for task in plan.tasks if task.action == "blocked"]
    if blocked:
        raise WorkflowError("; ".join(f"Task {task.name}: {task.reason}" for task in blocked))
    for task in plan.tasks:
        if task.action == "deferred":
            logger.info("Task %s: %s", task.name, task.reason)
    if dry_run:
        for task in plan.tasks:
            if task.action in ("fresh", "initialize"):
                logger.info("Task %s: %s", task.name, task.reason)
            for local in task.pending:
                logger.info("Would submit %s__%s", task.name, local)
            if task.action == "prepare":
                logger.info("Would restart preparation in the same attempt")
        return
    replacements = [task for task in plan.tasks if task.action in ("fresh", "initialize") and task.attempt is not None]
    transfers = [task for task in plan.tasks if task.action in ("transfer", "repair")]
    needed_roots = {"results", "staging"} if any(task.pending for task in plan.tasks) else set()
    if any(task.action in ("fresh", "initialize") for task in plan.tasks):
        needed_roots.add("work")
    recreate = sorted(key for key in needed_roots if plan.store.root_needs_recreation(key))
    if replacements or transfers or recreate:
        with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
            consumers = admission.consumer_activity(plan.store, backend, ctx.backend)
            for task in [*replacements, *transfers]:
                activity = admission.replacement_activity(plan.store, task.attempt, backend, ctx.backend, consumers)
                if activity.reason:
                    raise WorkflowError(f"Task {task.name}: {activity.reason}")
                if task.action == "transfer":
                    transfer.inspect(plan.store, task.attempt)
                elif task.action == "repair":
                    transfer.repair_sources(plan.store, task.attempt)
                elif plan.store.result_removal(task.attempt) != task.removal:
                    raise WorkflowError(f"Task {task.name}: results ownership changed after planning")
            if recreate:
                for attempt in plan.store.recorded_tasks():
                    activity = admission.replacement_activity(plan.store, attempt, backend, ctx.backend, consumers)
                    if activity.reason:
                        raise WorkflowError("Cannot recreate " + ", ".join(recreate) + " root: " + activity.reason)
        if recreate:
            for key in recreate:
                plan.store.recreate_root(key)
    selected = []
    for task in plan.tasks:
        if task.action in ("fresh", "initialize", "prepare", "continue", "retry", "transfer", "repair"):
            attempt = task.attempt
            if task.action == "fresh":
                attempt = plan.store.initialize(task, workflow._result_dirs[task.name],
                                                tracking=ctx.config.get("use_spec_hashes"),
                                                managed_tmpdir=workflow.managed_tmpdir,
                                                producers={name: plan.store.recorded_current(name)["attempt"]
                                                           for name in producer_names(task.structure)})
            elif task.action == "initialize":
                attempt = plan.store.finish_initialization(attempt)
            elif task.action == "repair":
                attempt = plan.store.start_repair(attempt, transfer.repair_sources(plan.store, attempt))
            selected.append((task, attempt))
    if not selected:
        return
    submitted = {task.name: dict(task.submissions) for task in plan.tasks}
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        with get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config) as hashes:
            for task, attempt in selected:
                observed = {} if task.action == "fresh" else dict(task.submissions)
                if task.action == "repair":
                    observed = admission.observe(plan.store, attempt, backend, ctx.backend)
                submitted[task.name] = observed
                if task.retry:
                    observed = admission.observe(plan.store, attempt, backend, ctx.backend)
                    if any(item.state == "uncertain" for item in observed.values()):
                        raise WorkflowError(f"Task {task.name}: unresolved submission prevents retry")
                    admission.require_retry_inactive(observed, task.retry)
                    if task.action != "prepare" or _files.exists(plan.store.attempt_dir(attempt) / "inputs.json"):
                        plan.store.check_inputs(attempt)
                    attempt = plan.store.replace_executions(task, tracking=ctx.config.get("use_spec_hashes"))
                    submitted[task.name] = observed
                admission.restore_tracking(backend, ctx.backend, observed)
                for item in observed.values():
                    if item.reconcile:
                        admission.acknowledge(plan.store, attempt, item.intent, item.job_id)
                for local in task.pending:
                    plan.store.validate_roots()
                    dependencies, references = [], []
                    if local == "gwflow_prepare":
                        for name in attempt["producers"]:
                            previous = submitted[name].get("gwflow_complete")
                            if previous is None or previous.state in ("pending", "uncertain", "failed", "cancelled"):
                                raise WorkflowError(f"Producer {name} has no available Completion submission")
                            if previous.state != "complete":
                                if previous.job_id is None:
                                    raise WorkflowError(f"Unresolved producer Completion submission: {previous.submission}")
                                admission.restore_tracking(backend, ctx.backend, {name: previous})
                                dependencies.append(Target(previous.submission, [], [], {}))
                    for previous in admission.predecessors(attempt, local):
                        item = observed[previous]
                        references.append({"local": previous, **admission.identity(item.intent)})
                        if item.state != "complete":
                            if item.job_id is None:
                                raise WorkflowError(f"Unresolved dependency submission: {item.submission}")
                            dependencies.append(Target(item.submission, [], [], {}))
                    intent = admission.new_intent(plan.store, attempt, local, references, backend, ctx.backend)
                    target = _job(plan.store, attempt, local, workflow, intent)
                    _log_aliases(ctx.working_dir, f"{attempt['task']}__{local}", target.name)
                    observed[local] = admission.submit(plan.store, attempt, intent, target, dependencies, backend, hashes)
