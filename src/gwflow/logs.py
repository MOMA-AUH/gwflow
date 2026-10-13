"""Discover Task logs and read individual job streams without planning work."""

import shlex
import signal
from contextlib import ExitStack, contextmanager

import click
from gwf.backends import BackendStatus, create_backend
from gwf.core import pass_context
from gwf.plugins.logs import logs as gwf_logs

from . import admission
from ._frontend import _submission_guard, load_workflow
from .lifecycle import Store
from .presentation import inspection_command
from .selection import task_selection
from .workflow import lifecycle_jobs


class _ObservationTimeout(Exception):
    """Escape backend retries that catch OSError (including TimeoutError)."""


@contextmanager
def _observation_deadline():
    # gwf's local client retries a missing worker for hours. Log discovery
    # must return using disk evidence even when a backend call never returns.
    def expired(signum, frame):
        raise _ObservationTimeout("scheduler observation timed out")

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)


def _display_index(store, name, attempt, jobs, declared, unavailable=None):
    context = f"attempt {attempt['attempt']}" if attempt else "no recorded work"
    if not declared:
        context += "; no longer declared"
    click.echo(f"Task {name}: {context}")
    if unavailable:
        click.echo(f"  live state unavailable: {unavailable}")
    for local, item in sorted(jobs.items(), key=lambda pair: pair[1].state != "failed"):
        live = "; live state unavailable" if item.intent and item.backend_state == BackendStatus.UNKNOWN else ""
        state = ({BackendStatus.SUBMITTED: "queued", BackendStatus.RUNNING: "running"}.get(item.backend_state, item.state)
                 if item.state == "active" else item.state)
        click.echo(f"  {name}__{local}: {state}; submission {item.submission or 'not submitted'}{live}")
        if item.job_id is not None:
            click.echo(f"    Backend job: {item.job_id}")
        for stream in ("stdout", "stderr"):
            path = store.working_dir / ".gwf" / "logs" / f"{item.submission}.{stream}"
            available = item.submission is not None and path.is_file()
            click.echo(f"    {stream}: {'available' if available else 'missing'}")
            if available:
                options = ["--stderr"] if stream == "stderr" else []
                click.echo("      " + shlex.join(inspection_command("logs", item.submission, *options, "--no-pager")))
    if declared:
        click.echo("  Next action and reason: " + shlex.join(inspection_command("run", name, "--dry-run", "--details")))
    else:
        click.echo("  Restore this Task's declaration before using the run preview for its Next action and reason.")


def _job_names(store, workflow):
    names = {name: "declared public job" for name in workflow.targets}
    for attempt in store.recorded_attempts():
        for local in lifecycle_jobs(attempt["structure"]["targets"]):
            names[f"{attempt['task']}__{local}"] = f"recorded public job in Task {attempt['task']!r}"
        for intent in admission.recorded_intents(store, attempt):
            names[intent["submission"]] = f"recorded submission in Task {attempt['task']!r}"
    return names


@click.command()
@click.argument("selectors", nargs=-1)
@click.option("-e", "--stderr", is_flag=True)
@click.option("--no-pager", is_flag=True)
@pass_context
def logs(ctx, selectors, stderr, no_pager):
    """Index current logs for Task names or quoted globs; omit selectors for all Tasks.

    One exact public job or submission name reads its individual log instead.
    Use --stderr only with an individual job; indexes show both streams.
    """
    workflow = load_workflow(ctx)
    try:
        with _submission_guard(ctx.working_dir):
            store = Store.for_workflow(workflow)
            attempts = {attempt["task"]: attempt for attempt in store.recorded_tasks()}
            names = set(workflow._task_declarations) | set(attempts)
            job_names = _job_names(store, workflow)
            if len(selectors) == 1 and selectors[0] in job_names and selectors[0] not in names:
                target = selectors[0]
                click.get_current_context().invoke(gwf_logs, target=target, stderr=stderr, no_pager=no_pager)
                return
            selected = task_selection(names, selectors)
            for name in sorted(selected & job_names.keys()):
                raise click.UsageError(f"Ambiguous log name {name!r}: both a Task and a {job_names[name]}.")
            if stderr:
                raise click.UsageError("--stderr requires one exact individual job or submission name; Task indexes show both streams.")
            observations = {}
            unavailable = None
            recorded = [name for name in sorted(selected) if name in attempts]
            if recorded:
                try:
                    # Cancel the alarm before backend cleanup can save tracking.
                    with ExitStack() as cleanup, _observation_deadline():
                        backend = cleanup.enter_context(create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config))
                        observations = {name: admission.observe(store, attempts[name], backend, ctx.backend) for name in recorded}
                except Exception as error:
                    # Disk evidence remains inspectable during a scheduler
                    # outage. UNKNOWN observations never establish inactivity.
                    unavailable = str(error)
                    observations = {name: admission.observe(store, attempts[name], None, ctx.backend) for name in recorded}
            for name in sorted(selected):
                _display_index(store, name, attempts.get(name), observations.get(name, {}),
                               name in workflow._task_declarations, unavailable)
            if not selected:
                click.echo("No Tasks selected")
    except OSError as error:
        raise click.ClickException(str(error)) from error
