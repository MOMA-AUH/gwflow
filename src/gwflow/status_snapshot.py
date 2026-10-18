"""Acquire serializable status observations in a disposable CLI process."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shlex
import signal
import time

import click
from gwf.core import pass_context

from ._frontend import _submission_guard, load_workflow
from .planning import plan_workflow
from .inspection import task_details
from .presentation import NEXT_ACTIONS, Report, inspection_command, observed_job_state, selected_rows, task_groups, task_order
from .status import status_selection


@click.command(name="status")
@click.option("--snapshot-file", "destination", type=click.Path(path_type=Path), required=True, hidden=True)
@click.argument("selectors", nargs=-1)
@pass_context
def capture(ctx, destination, selectors):
    workflow = load_workflow(ctx)
    with _submission_guard(ctx.working_dir):
        names = status_selection(workflow, selectors)
        plan = plan_workflow(workflow, ctx, selectors=names)
        rows = selected_rows(workflow, plan, names=names)
        tasks = []
        for row in rows:
            task = row.task
            jobs = []
            for local, state in row.jobs.items():
                item = task.submissions.get(local)
                submission = item.submission if item else None
                jobs.append({"local": local, "name": f"{task.name}__{local}", "state": state,
                             "observed_state": observed_job_state(task, local) if submission else state,
                             "requires_repetition": bool(submission and (task.restart_required or local in task.repeated_jobs)),
                             "submission": submission, "backend_id": item.job_id if item else None,
                             "logs": {stream: str(plan.store.working_dir / ".gwf" / "logs" / f"{submission}.{stream}")
                                      for stream in ("stdout", "stderr")} if submission else {}})
            tasks.append({"name": task.name, "state": row.state, "completed": row.completed,
                          "total": row.total, "detail": row.detail, "jobs": jobs,
                          "next_action": NEXT_ACTIONS[task.action], "reason": task.reason,
                          "diagnostics": "\n".join(task_details(plan.store, task)),
                          "commands": [shlex.join(inspection_command(*args)) for args in (
                              ("run", task.name, "--dry-run"), ("run", task.name), ("cancel", task.name))]})
        with redirect_stdout(io.StringIO()) as notices:
            Report(plan.store, plain=True).workflow_notices(task_order(workflow, plan), rows, include_reasons=True)
        snapshot = {
            "finished": time.time(),
            "total": len(workflow._task_declarations),
            "tasks": tasks,
            "notices": notices.getvalue().strip(),
            "groups": [{"label": group.label, "members": [name for name in group.members if name in names]}
                       for group in task_groups(workflow, plan)],
        }
    destination.write_text(json.dumps(snapshot))


if __name__ == "__main__":
    # This transport command exists only in the inspection child, not in the
    # public CLI. Loading goes through the same context and error handling.
    from .cli import main
    # Let context managers remove owned acquisition files and release locks.
    # The parent still kills the process group if authored code will not stop.
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    main.add_command(capture)
    main()
