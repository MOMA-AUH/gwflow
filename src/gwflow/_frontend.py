"""Coordinate frontend operations until their bookkeeping is saved."""

from contextlib import contextmanager
import fcntl
import signal
from pathlib import Path
import sys
import tempfile

import click
from gwf import Workflow as GwfWorkflow
from gwf.core import Context
from gwf.exceptions import WorkflowError

from .activity import activity


class _ObservationTimeout(click.ClickException):
    """Escape backend retries that catch OSError (including TimeoutError)."""


@contextmanager
def observation_deadline():
    # gwf's local client retries a missing worker for hours. Frontends that
    # can use disk evidence need bounded observation without timing out writes.
    def expired(signum, frame):
        raise _ObservationTimeout("scheduler observation timed out")

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)


class ManagedContext(Context):
    """Identify the managed executable without changing gwf command objects."""


@contextmanager
def _fresh_source(cache_parent):
    # Disabling writes alone still permits timestamp/size-valid stale bytecode.
    # Use an empty namespace for all imports made by the workflow, then restore
    # the interpreter settings. Inspection children keep it in their owned tree.
    with tempfile.TemporaryDirectory(prefix="gwflow-source-", dir=cache_parent) as directory:
        previous = sys.pycache_prefix, sys.dont_write_bytecode
        sys.pycache_prefix = str(Path(directory) / "pycache")
        sys.dont_write_bytecode = True
        try:
            yield
        finally:
            sys.pycache_prefix, sys.dont_write_bytecode = previous


def load_workflow(ctx, *, cache_parent=None):
    from .workflow import Workflow

    if not ctx.workflow_file.is_file():
        raise WorkflowError(f"Could not find workflow file {ctx.workflow_file}; use --file PATH[:OBJECT] to select one.")
    with activity("Inspecting workflow..."), _fresh_source(cache_parent):
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
            if waiting_message is None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:
                with activity(waiting_message):
                    fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
