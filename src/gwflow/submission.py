"""Initialize and admit only the jobs selected by a managed lifecycle plan."""

import logging
import os
import shlex
import sys
from uuid import uuid4

from gwf import Target
from gwf.backends import create_backend
from gwf.core import get_spec_hashes
from gwf.exceptions import WorkflowError
from gwf.scheduling import submit_backend

from . import _files

from .workflow import lifecycle_jobs


logger = logging.getLogger(__name__)


def _job(store, attempt, local, workflow):
    finishing = local == "gwflow_complete"
    preparing = local == "gwflow_prepare"
    computing = not (finishing or preparing)
    declaration = workflow._task_declarations[attempt["task"]]
    command = [sys.executable, "-m", "gwflow.execution", str(store.owner_path),
               attempt["task"], attempt["attempt"], "finish" if finishing else "prepare" if preparing else "execute"]
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
    return Target(name=attempt["jobs"][local], inputs=[], outputs=[], options=dict(options),
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
            if task.action in ("fresh", "prepare"):
                for local in lifecycle_jobs(task.structure["targets"]):
                    logger.info("Would submit %s__%s", task.name, local)
                if task.action == "prepare":
                    logger.info("Would restart preparation in the same attempt")
        return
    attempts = []
    for task in plan.tasks:
        if task.action in ("fresh", "prepare"):
            attempt = task.attempt if task.action == "prepare" else plan.store.initialize(task, workflow._result_dirs[task.name],
                                            tracking=ctx.config.get("use_spec_hashes"),
                                            managed_tmpdir=workflow.managed_tmpdir)
            attempts.append(attempt)
    if not attempts:
        return
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        with get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config) as hashes:
            for attempt in attempts:
                dependencies = []
                for local in lifecycle_jobs(attempt["executions"]):
                    plan.store.validate_roots()
                    target = _job(plan.store, attempt, local, workflow)
                    identity = {**plan.store.job_identity(attempt, local), "admission": uuid4().hex}
                    plan.store.publish(attempt, f"submissions/{local}-intent.json", "submission-intent", **identity)
                    _log_aliases(ctx.working_dir, f"{attempt['task']}__{local}", target.name)
                    submit_backend(target, dependencies, backend, hashes)
                    tracked_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
                    plan.store.publish(attempt, f"submissions/{local}-ack.json", "submission-ack", **identity, job_id=tracked_id)
                    dependencies.append(target)
