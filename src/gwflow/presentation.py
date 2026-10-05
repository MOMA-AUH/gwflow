"""Static Task views derived from planning observations, without backend access."""

from collections import Counter
from dataclasses import dataclass

import click
from gwf.backends import BackendStatus

from .lifecycle import TaskObservation, producer_names
from .workflow import lifecycle_jobs


STATES = ("pending", "queued", "preparing", "running", "finishing", "reusable",
          "repairable", "deferred", "failed", "canceled", "blocked")
ACTIVE = ("preparing", "running", "finishing")


@dataclass
class TaskRow:
    task: TaskObservation
    state: str
    completed: int | None
    total: int
    detail: str
    jobs: dict[str, str]

    @property
    def progress(self):
        return f"{'?' if self.completed is None else self.completed}/{self.total}"


def task_order(workflow, plan):
    """Choose the earliest declared eligible Task at each dependency step."""
    remaining = {name: next(task for task in plan.tasks if task.name == name)
                 for name in workflow._task_declarations}
    while remaining:
        name = next(name for name, task in remaining.items()
                    if not set(producer_names(task.structure)).intersection(remaining))
        yield remaining.pop(name)


def job_states(task):
    states = {}
    for local in lifecycle_jobs(task.structure["targets"]):
        item = task.submissions.get(local)
        if task.action == "reuse":
            state = "completed"
        elif item is not None and item.state == "active":
            state = "queued" if item.backend_state == BackendStatus.SUBMITTED else "running"
        elif task.restart_required:
            state = "pending"
        elif item is None:
            state = "pending" if task.observations_available else "unknown"
        else:
            state = {"complete": "completed", "cancelled": "canceled", "uncertain": "unknown"}.get(item.state, item.state)
        if state == "completed" and local in task.repeated_jobs:
            state = "pending"
        states[local] = state
    return states


def task_row(task):
    jobs = job_states(task)
    counts = Counter(jobs.values())
    # A declaration edit can remove a job that is still protecting the old
    # attempt. Its activity belongs in Detail, outside the new denominator.
    for local, item in task.submissions.items():
        if local not in jobs and item.state == "active":
            counts["queued" if item.backend_state == BackendStatus.SUBMITTED else "running"] += 1
    if task.action == "blocked":
        state = "blocked"
    elif task.action == "reuse":
        state = "reusable"
    elif counts["failed"]:
        state = "failed"
    elif counts["canceled"]:
        state = "canceled"
    elif jobs["gwflow_prepare"] == "running":
        state = "preparing"
    elif jobs["gwflow_complete"] == "running":
        state = "finishing"
    elif counts["running"]:
        state = "running"
    elif counts["queued"]:
        state = "queued"
    else:
        state = {"repair": "repairable", "deferred": "deferred"}.get(task.action, "pending")
    completed = counts["completed"]
    if not task.restart_required and task.action != "reuse" and (
            not task.observations_available or counts["unknown"]):
        completed = None
    detail = []
    if task.action == "blocked":
        detail.append(blocking_reason(task.reason))
    elif task.action == "reuse":
        detail.append("work present" if task.work_present else "work cleaned")
    elif task.action == "repair":
        detail.append("retained results need repair")
    elif task.action == "transfer":
        detail.append("results transfer needs recovery")
    if task.action == "deferred":
        detail.append("run again after upstream result recovery")
    if task.restart_required and task.attempt:
        detail.append("fresh attempt required")
    if task.action in ("retry", "prepare"):
        detail.append("retry available")
    for label in ("failed", "canceled", "running", "queued"):
        count = counts[label]
        if count:
            description = "still running" if label == "running" and state in ("blocked", "failed", "canceled") else label
            detail.append(f"{count} job{'s' if count != 1 else ''} {description}")
    if completed is None:
        detail.append("progress unavailable")
    return TaskRow(task, state, completed, len(jobs), "; ".join(detail), jobs)


def blocking_reason(reason):
    for fragment, concise in (
        ("unresolved submission:", "submission outcome unknown"),
        ("Active consumers block replacement:", "active consumers block replacement"),
        ("Unresolved or active work blocks replacement:", "active work blocks replacement"),
        ("Active dependent work blocks retry:", "active dependent jobs block retry"),
        ("Inputs unavailable;", "external input unavailable"),
        ("Missing external input:", "missing external input"),
    ):
        if reason.lower().startswith(fragment.lower()):
            return concise
    return reason


def summary(rows, total):
    count = len(rows)
    label = "Task" if count == 1 else "Tasks"
    shown = f"{count} {label} shown" if count == total else f"{count} of {total} Tasks shown"
    counts = Counter("active" if row.state in ACTIVE else row.state for row in rows)
    return shown + (": " + ", ".join(f"{value} {state}" for state, value in counts.items()) if counts else "")


def print_status(rows, total):
    click.echo(summary(rows, total))
    click.echo(f"{'Task':<33} {'State':<14} {'Jobs completed':<16} Detail")
    for row in rows:
        color = ("red" if row.state in ("failed", "canceled") else
                 "yellow" if row.state in ("blocked", "repairable", "deferred") else
                 "green" if row.state == "reusable" else
                 "blue" if row.state in ACTIVE else "cyan" if row.state == "queued" else "magenta")
        click.secho(f"{'Task ' + row.task.name:<33} {row.state:<14} {row.progress:<16} {row.detail}".rstrip(), fg=color)
