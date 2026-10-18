"""Discover Task logs and read individual job streams without planning work."""

import json
import shlex
from contextlib import ExitStack

import click
from gwf.backends import BackendStatus, create_backend
from gwf.core import pass_context
from gwf.plugins.logs import logs as gwf_logs

from .activity import activity
from .diagnostics import report_error

from . import admission
from ._frontend import _submission_guard, load_workflow, observation_deadline
from .lifecycle import Store
from .presentation import inspection_command
from .selection import task_selection
from .workflow import lifecycle_jobs


def _display_jobs(store, name, jobs, *, historical=False, details=False):
    indent = "    " if historical else "  "
    jobs = sorted(jobs, key=lambda item: item.state != "failed")
    for item in jobs:
        live = "; live state unavailable" if item.intent and item.backend_state == BackendStatus.UNKNOWN else ""
        state = ({BackendStatus.SUBMITTED: "queued", BackendStatus.RUNNING: "running"}.get(item.backend_state, item.state)
                 if item.state == "active" else item.state)
        if historical:
            state += " (historical)"
        identity = f"; submission {item.submission or 'not submitted'}" if details or historical else ""
        row = f"{indent}{name}__{item.local}: {state}{identity}{live}"
        streams = []
        for stream in ("stdout", "stderr"):
            path = store.working_dir / ".gwf" / "logs" / f"{item.submission}.{stream}"
            available = item.submission is not None and path.is_file()
            streams.append((stream, path, available))
        if not details and not historical:
            click.echo(row + "; " + "; ".join(f"{stream}: {'available' if available else 'missing'}"
                                            for stream, _, available in streams))
            continue
        click.echo(row)
        if details and item.intent:
            click.echo(f"{indent}  Backend: " + json.dumps(item.intent["backend"], sort_keys=True))
        if details and item.job_id is not None:
            click.echo(f"{indent}  Backend job: {item.job_id}")
        for stream, path, available in streams:
            click.echo(f"{indent}  {stream}: {'available' if available else 'missing'}")
            if details and item.submission:
                click.echo(f"{indent}    Path: {path}")
            if available:
                options = ["--stderr"] if stream == "stderr" else []
                click.echo(indent + "    " + shlex.join(inspection_command("logs", item.submission, *options, "--no-pager")))
    if not historical and jobs:
        public = f"{name}__{jobs[0].local}"
        click.echo("  Read a job's logs (stdout, then stderr):")
        for options in ((), ("--stderr",)):
            click.echo("    " + shlex.join(inspection_command("logs", public, *options, "--no-pager")))


def _display_index(store, name, attempt, jobs, declared, unavailable=None, history=(), *, details=False):
    context = "current attempt" if attempt else "no recorded work"
    if attempt and details:
        context += " " + attempt["attempt"]
    if not declared:
        context += "; no longer declared"
    click.echo(f"Task {name}: {context}")
    if unavailable:
        click.echo(f"  live state unavailable: {unavailable}")
    _display_jobs(store, name, jobs.values(), details=details)
    current = {item.submission for item in jobs.values()}
    for previous, submissions in history:
        same_attempt = previous["attempt"] == attempt["attempt"]
        historical = [item for item in submissions if not same_attempt or item.submission not in current]
        if historical or not same_attempt:
            context = "Historical submissions in current attempt" if same_attempt else "Historical attempt"
            click.echo(f"  {context} {previous['attempt']}:")
            _display_jobs(store, name, historical, historical=True, details=details)
            if not historical:
                click.echo("    No recorded submissions")
    click.echo("  Public job names follow the current submission; exact submission names keep the same log after a retry.")
    if not details:
        options = ["--history"] if history else []
        click.echo("  Exact submissions, paths, and commands: " +
                   shlex.join(inspection_command("logs", name, *options, "--details")))
    if declared:
        click.echo("  Next action and reason: " + shlex.join(inspection_command("run", name, "--dry-run", "--details")))
    else:
        click.echo("  Restore this Task's declaration before using the run preview for its Next action and reason.")


def _job_names(store, workflow):
    names = {f"{name}__{local}": ("declared public job", name)
             for name, task in workflow._task_declarations.items() for local in lifecycle_jobs(task.targets)}
    for attempt in store.recorded_attempts():
        for local in lifecycle_jobs(attempt["structure"]["targets"]):
            names[f"{attempt['task']}__{local}"] = (f"recorded public job in Task {attempt['task']!r}", attempt["task"])
        for intent in admission.recorded_intents(store, attempt):
            names[intent["submission"]] = (f"recorded submission in Task {attempt['task']!r}", attempt["task"])
    return names


@click.command()
@click.argument("selectors", nargs=-1)
@click.option("-e", "--stderr", is_flag=True)
@click.option("--no-pager", is_flag=True)
@click.option("--history", is_flag=True, help="Include earlier retry submissions and previous Task attempts.")
@click.option("--details", is_flag=True, help="Show full Task-index identities, paths, and exact-submission commands.")
@pass_context
def logs(ctx, selectors, stderr, no_pager, history, details):
    """Index current logs for Task names or quoted globs; omit selectors for all Tasks.

    One exact public job or submission name reads its individual log instead.
    Use --stderr only with an individual job; indexes show both streams.
    --history adds earlier submissions and attempts to Task indexes only.
    --details expands Task indexes with full identities, paths, and pinned commands.
    """
    workflow = load_workflow(ctx)
    try:
        with _submission_guard(ctx.working_dir):
            store = Store.for_workflow(workflow)
            attempts = {attempt["task"]: attempt for attempt in store.recorded_tasks()}
            names = set(workflow._task_declarations) | set(attempts)
            job_names = _job_names(store, workflow)
            if len(selectors) == 1 and selectors[0] in job_names and selectors[0] not in names:
                if history:
                    raise click.UsageError("--history requires a Task index, not an individual job or submission stream.")
                target = selectors[0]
                if details:
                    owner = job_names[target][1]
                    options = ["--stderr"] if stderr else []
                    if no_pager:
                        options.append("--no-pager")
                    index_command = shlex.join(inspection_command("logs", owner, "--details"))
                    stream_command = shlex.join(inspection_command("logs", target, *options))
                    raise click.UsageError("--details requires a Task index, not an individual job or submission stream.\n"
                                           f"Use {index_command} for identities and paths.\n"
                                           f"Read the stream with {stream_command}")
                click.get_current_context().invoke(gwf_logs, target=target, stderr=stderr, no_pager=no_pager)
                return
            selected = task_selection(names, selectors)
            for name in sorted(selected & job_names.keys()):
                raise click.UsageError(f"Ambiguous log name {name!r}: both a Task and a {job_names[name][0]}.")
            if stderr:
                raise click.UsageError("--stderr requires one exact individual job or submission name; Task indexes show both streams.")
            observations = {}
            historical = {}
            unavailable = None
            recorded = [name for name in sorted(selected) if name in attempts]
            archived = [attempt for attempt in store.recorded_attempts() if attempt["task"] in selected] if history else []
            if recorded:
                def inspect(backend):
                    current = {name: admission.observe(store, attempts[name], backend, ctx.backend) for name in recorded}
                    earlier = {attempt["attempt"]: admission.observe_submissions(store, attempt, backend, ctx.backend)
                               for attempt in archived}
                    return current, earlier

                try:
                    # Cancel the alarm before backend cleanup can save tracking.
                    with activity("Waiting for scheduler..."), ExitStack() as cleanup, observation_deadline():
                        backend = cleanup.enter_context(create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config))
                        observations, historical = inspect(backend)
                except Exception as error:
                    report_error(error, "Log inspection")
                    # Disk evidence remains inspectable during a scheduler
                    # outage. UNKNOWN observations never establish inactivity.
                    unavailable = str(error)
                    observations, historical = inspect(None)
            for name in sorted(selected):
                history_indexes = [(attempt, historical[attempt["attempt"]]) for attempt in archived if attempt["task"] == name]
                history_indexes.sort(key=lambda item: item[0]["attempt"] != attempts[name]["attempt"])
                _display_index(store, name, attempts.get(name), observations.get(name, {}),
                               name in workflow._task_declarations, unavailable, history_indexes, details=details)
            if not selected:
                click.echo("No Tasks selected")
    except OSError as error:
        raise click.ClickException(str(error)) from error
