"""Acquire serializable status observations in a disposable CLI process."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import signal
import sys
import time

import click
from gwf.core import pass_context

from ._frontend import _submission_guard, load_workflow
from .activity import observe_activity
from .planning import plan_workflow
from .presentation import NEXT_ACTIONS, Report, observed_job_state, selected_rows, task_groups, task_order
from .status import status_selection


@click.command(name="status")
@click.option("--snapshot-file", "destination", type=click.Path(path_type=Path), required=True, hidden=True)
@click.argument("selectors", nargs=-1)
@pass_context
def capture(ctx, destination, selectors):
    def publish_activity(state):
        path = destination.with_suffix('.activity.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        temporary.replace(path)

    with observe_activity(publish_activity):
        capture_snapshot(ctx, destination, selectors)


def capture_snapshot(ctx, destination, selectors):
    # Fresh source must not reuse timestamp/size-valid bytecode after a rapid
    # same-size edit. This private, empty cache namespace affects this child only.
    sys.pycache_prefix = str(destination.parent / "pycache")
    sys.dont_write_bytecode = True
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
            tasks.append({"name": task.name, "instance_key": workflow._task_naming[task.name].key,
                          "attempt": task.attempt['attempt'] if task.attempt else None,
                          "state": row.state, "completed": row.completed,
                          "total": row.total, "detail": row.detail, "jobs": jobs,
                          "next_action": NEXT_ACTIONS[task.action], "reason": task.reason})
        with redirect_stdout(io.StringIO()) as notices:
            Report(plan.store, plain=True).workflow_notices(task_order(workflow, plan), rows, include_reasons=True)
        snapshot = {
            "finished": time.time(),
            "storage": {"working_dir": str(plan.store.working_dir), "work_root": str(plan.store.locations['work']),
                        "results_root": str(plan.store.locations['results']),
                        "staging_root": str(plan.store.locations['staging'])},
            "total": len(workflow._task_declarations),
            "tasks": tasks,
            "notices": notices.getvalue().strip(),
            "groups": [{"label": group.label, "key": group.qualified,
                        "members": [name for name in group.members if name in names]}
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
