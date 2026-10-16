"""Coordinate frontend operations until their bookkeeping is saved."""

from contextlib import contextmanager
import fcntl
from pathlib import Path

import click
from gwf import Workflow as GwfWorkflow
from gwf.core import Context
from gwf.exceptions import WorkflowError


class ManagedContext(Context):
    """Identify the managed executable without changing gwf command objects."""


def load_workflow(ctx):
    from .workflow import Workflow

    if not ctx.workflow_file.is_file():
        raise WorkflowError(f"Could not find workflow file {ctx.workflow_file}; use --file PATH[:OBJECT] to select one.")
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        raise WorkflowError("Managed commands require a gwflow.Workflow; use gwf for ordinary workflows.")
    if not ctx.config.get("use_spec_hashes"):
        raise WorkflowError("Managed command tracking is mandatory; remove use_spec_hashes=false with "
                            "'gwflow config unset use_spec_hashes'.")
    return workflow


@contextmanager
def _submission_guard(working_dir, *, waiting_message="Waiting for frontend submission bookkeeping..."):
    # gwf saves command hashes and backend tracking on context exit. Keep
    # concurrent runs out until those writes finish, not just until planning
    # or submission returns. The OS releases this lock on CLI interruption;
    # jobs neither inherit nor wait for it.
    path = Path(working_dir) / ".gwf" / "gwflow-submission.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if waiting_message is not None:
                click.echo(waiting_message, err=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
