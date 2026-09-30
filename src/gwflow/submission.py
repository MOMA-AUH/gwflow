"""Initialize and admit only the jobs selected by a managed lifecycle plan."""

import logging
import os
import shlex
import sys

from gwf import Target
from gwf.backends import create_backend
from gwf.core import get_spec_hashes
from gwf.exceptions import WorkflowError
from gwf.scheduling import submit_backend

from . import _files


logger = logging.getLogger(__name__)


def _job(store, attempt, local, workflow):
    finishing = local == "gwflow_complete"
    declaration = workflow._task_declarations[attempt["task"]]
    command = [sys.executable, "-m", "gwflow.execution", str(store.owner_path),
               attempt["task"], attempt["attempt"], "finish" if finishing else "execute"]
    if not finishing:
        command.append(local)
    options = ({**workflow.defaults, **workflow.completion_defaults} if finishing
               else declaration.targets[local].options)
    kwargs = {}
    if not finishing and declaration.targets[local].executor is not None:
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
            if task.action == "fresh":
                for local in [*task.structure["targets"], "gwflow_complete"]:
                    logger.info("Would submit %s__%s", task.name, local)
        return
    fresh = []
    for task in plan.tasks:
        if task.action == "fresh":
            attempt = plan.store.initialize(task, workflow._result_dirs[task.name],
                                            tracking=ctx.config.get("use_spec_hashes"),
                                            managed_tmpdir=workflow.managed_tmpdir)
            fresh.append(attempt)
    if not fresh:
        return
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        with get_spec_hashes(working_dir=ctx.working_dir, config=ctx.config) as hashes:
            for attempt in fresh:
                dependencies = []
                for local in [*attempt["executions"], "gwflow_complete"]:
                    plan.store.validate_roots()
                    target = _job(plan.store, attempt, local, workflow)
                    identity = plan.store.job_identity(attempt, local)
                    plan.store.publish(attempt, f"submissions/{local}-intent.json", "submission-intent", **identity)
                    _log_aliases(ctx.working_dir, f"{attempt['task']}__{local}", target.name)
                    submit_backend(target, dependencies if local == "gwflow_complete" else [], backend, hashes)
                    tracked_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
                    plan.store.publish(attempt, f"submissions/{local}-ack.json", "submission-ack", **identity, job_id=tracked_id)
                    dependencies.append(target)
