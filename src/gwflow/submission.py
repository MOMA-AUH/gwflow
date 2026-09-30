"""Initialize and admit only the jobs selected by a managed lifecycle plan."""

import logging
import os
import shlex
import sys

from gwf import Target
from gwf.backends import create_backend
from gwf.core import get_spec_hashes
from gwf.exceptions import WorkflowError

from . import _files, admission

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
    if dry_run:
        for task in plan.tasks:
            for local in task.cancel:
                logger.info("Would request cancellation of queued dependent %s__%s", task.name, local)
            for local in task.pending:
                logger.info("Would submit %s__%s", task.name, local)
            if task.action == "prepare":
                logger.info("Would restart preparation in the same attempt")
        return
    selected = []
    for task in plan.tasks:
        if task.action in ("fresh", "prepare", "continue", "retry"):
            attempt = task.attempt
            if task.action == "fresh":
                attempt = plan.store.initialize(task, workflow._result_dirs[task.name],
                                                tracking=ctx.config.get("use_spec_hashes"),
                                                managed_tmpdir=workflow.managed_tmpdir)
            selected.append((task, attempt))
    if not selected:
        return
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        with get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config) as hashes:
            for task, attempt in selected:
                observed = dict(task.submissions)
                if task.retry:
                    observed = admission.cancel_queued(plan.store, attempt, task.cancel, backend, ctx.backend)
                    if any(item.state == "uncertain" for item in observed.values()):
                        raise WorkflowError(f"Task {task.name}: unresolved submission prevents retry")
                    if any(observed[local].state == "active" for local in [*task.retry, "gwflow_complete"]):
                        raise WorkflowError(f"Task {task.name}: active dependent work blocks retry")
                    if task.action != "prepare" or _files.exists(plan.store.attempt_dir(attempt) / "inputs.json"):
                        plan.store.check_inputs(attempt)
                    attempt = plan.store.replace_executions(task, tracking=ctx.config.get("use_spec_hashes"))
                admission.restore_tracking(backend, ctx.backend, observed)
                for item in observed.values():
                    if item.reconcile:
                        admission.acknowledge(plan.store, attempt, item.intent, item.job_id)
                for local in task.pending:
                    plan.store.validate_roots()
                    dependencies, references = [], []
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
