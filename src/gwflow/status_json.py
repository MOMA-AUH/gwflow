"""The public JSON status report, separate from the browser transport."""

from contextlib import contextmanager, redirect_stdout
import json
import os
import sys

import click

from .diagnostics import error_message
from .presentation import NEXT_ACTIONS, needs_later_finish


@contextmanager
def diagnostics_on_stderr():
    """Include inherited child-process output in the diagnostic stream."""
    original = sys.stdout
    original.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        try:
            sys.stderr.flush()
            original.flush()
        finally:
            os.dup2(saved, 1)
            os.close(saved)


def emit_error(error):
    usage = isinstance(error, click.UsageError)
    message = error_message(error, "status")
    click.echo(json.dumps({"schema_version": 1, "error": {
        "code": "usage_error" if usage else "inspection_failed", "message": message}}))
    click.echo("Error: " + message, err=True)
    raise click.exceptions.Exit(2 if usage else 1)


def json_requested(args):
    options = args[:args.index("--")] if "--" in args else args
    return any(argument.partition("=")[0] == "--json" for argument in options)


class StatusCommand(click.Command):
    """Keep status parser errors inside its opt-in JSON error boundary."""

    def make_context(self, info_name, args, parent=None, **extra):
        requested = json_requested(args)
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as error:
            if not requested:
                raise
            emit_error(error)

    def invoke(self, ctx):
        if not ctx.params.get("json_report"):
            return super().invoke(ctx)
        try:
            with diagnostics_on_stderr():
                document = super().invoke(ctx)
        except (click.Abort, click.exceptions.Exit):
            raise
        except Exception as error:
            emit_error(error)
        click.echo(json.dumps(document))


def workflow_notices(tasks):
    tasks = list(tasks)
    notices = []

    def add(code, message, affected):
        if affected:
            notices.append({"code": code, "message": message,
                            "task_names": [task.name for task in affected]})

    add("workflow_blocked", "Blocking Tasks prevent new submissions within this Execution scope. "
        "Already active jobs may still be running.", [task for task in tasks if task.action == "blocked"])
    add("upstream_recovery_pending", "Run again after upstream result recovery.",
        [task for task in tasks if task.action == "deferred"])
    add("completion_followup_required", "Run again after the previous queued completion job settles.",
        [task for task in tasks if needs_later_finish(task)])
    add("submission_investigation_required", "Investigate the scheduler and job logs. Establish whole-Task "
        "inactivity, including computation, preparation, and completion jobs, before using gwflow recover. "
        "Cancellation requests alone do not establish inactivity. Recovery is not necessarily required "
        "and this notice does not establish that it is safe.",
        [task for task in tasks if any(item.state == "uncertain" for item in task.submissions.values())])
    return notices


def status_document(rows, tasks):
    return {"schema_version": 1, "tasks": [
        {"name": row.task.name,
         "attempt_id": row.task.attempt["attempt"] if row.task.attempt else None,
         "state": row.state, "next_action": NEXT_ACTIONS[row.task.action].lower(),
         "progress": {"completed": row.completed, "total": row.total},
         "reason": {"code": row.task.reason_code, "message": row.task.reason}}
        for row in rows], "notices": workflow_notices(tasks)}
