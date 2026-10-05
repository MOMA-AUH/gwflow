"""Static Task views derived from planning observations, without backend access."""

from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
import sys

import click
from gwf.backends import BackendStatus
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .inspection import blockage_line, condition, task_details
from .lifecycle import TaskObservation, ordered_targets, producer_names
from .workflow import lifecycle_jobs


STATES = ("pending", "queued", "preparing", "running", "finishing", "reusable",
          "repairable", "deferred", "failed", "canceled", "blocked")
ACTIVE = ("preparing", "running", "finishing")
NEXT_ACTIONS = {
    "fresh": "Run", "initialize": "Run", "continue": "Continue",
    "retry": "Retry", "prepare": "Retry", "transfer": "Finish", "repair": "Repair",
    "reuse": "Reuse", "active": "Wait", "deferred": "Defer", "blocked": "Blocked",
}


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


def plan_reason(task):
    """Keep the treatment legible; full planner diagnostics remain in details."""
    if task.action == "blocked":
        concise = blocking_reason(task.reason)
        if concise != task.reason:
            return concise + "; resolve before proceeding"
        if task.reason.startswith("Target ") and ": image unavailable for " in task.reason:
            return "image unavailable; check image access"
        return "validation prevents proceeding; see --details"
    if task.action == "fresh":
        return task.reason.split(";", 1)[0].split(":", 1)[0]
    if task.action == "retry":
        reason = "repeat failed, canceled, or invalid jobs; retain valid completed work"
        if "run again afterward" in task.reason:
            reason += "; run again after the queued completion job settles"
        return reason
    return {
        "initialize": "resume selected initialization before computation",
        "continue": "submit remaining jobs without repeating admitted work",
        "prepare": "preparation interrupted; restart in the same attempt",
        "repair": "retained results missing or changed; restore from valid work",
        "deferred": "run again after upstream result recovery",
        "active": "jobs queued or running; no new submission needed now",
    }.get(task.action, task.reason)


def summary(rows, total):
    count = len(rows)
    label = "Task" if count == 1 else "Tasks"
    shown = f"{count} {label} shown" if count == total else f"{count} of {total} Tasks shown"
    counts = Counter("active" if row.state in ACTIVE else row.state for row in rows)
    return shown + (": " + ", ".join(f"{value} {state}" for state, value in counts.items()) if counts else "")


def matches(name, patterns):
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def selected_rows(workflow, plan, *, targets=(), endpoints=False, statuses=(), group=()):
    producers = {name for task in plan.tasks for name in producer_names(task.structure)}
    rows = []
    for task in task_order(workflow, plan):
        if endpoints and task.name in producers:
            continue
        names = lifecycle_jobs(task.structure["targets"])
        if targets and not (matches(task.name, targets)
                            or any(matches(f"{task.name}__{local}", targets) for local in names)):
            continue
        if group and not any(matches(workflow.targets[f"{task.name}__{local}"].group or "none", group)
                             for local in task.structure["targets"]):
            continue
        row = task_row(task)
        if not statuses or row.state in statuses:
            rows.append(row)
    return rows


_VISUALS = {
    "pending": ("○", "magenta"), "queued": ("◷", "cyan"),
    "preparing": ("↻", "blue"), "running": ("▶", "blue"), "finishing": ("↗", "blue"),
    "reusable": ("✓", "green"), "completed": ("✓", "green"),
    "repairable": ("◇", "yellow"), "deferred": ("…", "yellow"),
    "failed": ("✗", "bold red"), "canceled": ("⊘", "red"),
    "blocked": ("!", "bold yellow"), "unknown": ("?", "yellow"),
}
_LIFECYCLE_NAMES = {"gwflow_prepare": "[preparation]", "gwflow_complete": "[completion]"}


class Report:
    """Print a static snapshot; terminal controls never change observations."""

    def __init__(self, store, *, plain=False, no_truncate=False):
        self.store = store
        stream = sys.stdout
        self.terminal = stream.isatty()
        try:
            ("╭╮╰╯│─━╸╺" + "".join(symbol for symbol, _ in _VISUALS.values())).encode(stream.encoding or "utf-8")
            self.unicode = True
        except UnicodeEncodeError:
            self.unicode = False
        self.plain = plain or not self.terminal or not self.unicode or os.environ.get("TERM", "").lower() in ("dumb", "unknown")
        self.truncate = self.terminal and not no_truncate
        root = click.get_current_context().find_root()
        no_color = root.params.get("no_color")
        if no_color is None:
            no_color = root.obj.config.get("no_color")
        if no_color is None:
            no_color = bool(os.environ.get("NO_COLOR"))
        self.console = Console(file=stream, force_terminal=self.terminal and not self.plain,
                               color_system=None if self.plain or no_color else "auto",
                               no_color=self.plain or no_color, markup=False, highlight=False)

    def text(self, value, *, style="", complete=False):
        return Text(value, style=style, no_wrap=self.truncate and not complete,
                    overflow="ellipsis" if self.truncate and not complete else "fold")

    def line(self, value, *, complete=False, style=""):
        if not self.terminal:
            click.echo(value, color=False)
        else:
            truncate = self.truncate and not complete
            text = self.text(value, complete=complete, style=style if not self.plain else "")
            if not self.unicode:
                text = Text(value.encode(self.console.encoding, errors="backslashreplace").decode(self.console.encoding))
                if truncate and text.cell_len > self.console.width:
                    text.truncate(max(1, self.console.width - 3), overflow="crop")
                    text.append("...")
            self.console.print(text, no_wrap=truncate, overflow="ellipsis" if truncate and self.unicode else "fold")

    def notice(self, message):
        if self.plain:
            self.line(message, complete=True)
        else:
            self.console.print(Panel(self.text(message, complete=True), border_style="yellow", title="Notice"))

    def state(self, name):
        symbol, style = _VISUALS[name]
        return Text(f"{symbol} {name}", style=style, no_wrap=True)

    def status(self, rows, total, *, expand=False):
        heading = summary(rows, total)
        if self.plain:
            self.line(heading, complete=True)
            self.line("Task / State / Jobs completed / Detail" if self.terminal else
                      f"{'Task':<33} {'State':<14} {'Jobs completed':<16} Detail", complete=True)
            for row in rows:
                if self.terminal:
                    self._compact_row(row)
                else:
                    self.line(f"{'Task ' + row.task.name:<33} {row.state:<14} {row.progress:<16} {row.detail}".rstrip())
        else:
            self.console.print(Panel(Text(heading, style="bold cyan"), border_style="cyan", title="Task status"))
            if self.console.width < 64:
                self.line("Jobs completed (steps)", complete=True)
                for row in rows:
                    self._compact_row(row)
            else:
                table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
                table.add_column("Task", ratio=2, min_width=12)
                table.add_column("State", width=12, no_wrap=True)
                table.add_column("Jobs completed", width=14, justify="right", no_wrap=True)
                bars = self.console.width >= 90
                if bars:
                    table.add_column("", width=min(14, (self.console.width - 80) // 2))
                table.add_column("Detail", ratio=3)
                for row in rows:
                    cells = [self.text(row.task.name), self.state(row.state), Text(row.progress)]
                    if bars:
                        cells.append(ProgressBar(total=row.total, completed=row.completed,
                                                 complete_style=_VISUALS[row.state][1], finished_style="green")
                                     if row.completed is not None else Text(""))
                    cells.append(self.text(row.detail, complete=row.task.action == "deferred"))
                    table.add_row(*cells, style="on grey11" if row.state in ("blocked", "failed") else None)
                self.console.print(table)
        if expand:
            for row in rows:
                self.details(row)

    def _compact_row(self, row):
        self.line("Task " + row.task.name)
        if self.plain:
            self.line(f"  {row.state}  {row.progress}", complete=True)
        else:
            text = self.state(row.state)
            text.append("  " + row.progress)
            self.console.print(text, no_wrap=False, overflow="fold")
        if row.detail:
            self.line("  " + row.detail, complete=row.task.action == "deferred")

    def plan(self, plan, rows, *, expand=False, preview=True):
        self.line("Managed whole-workflow plan", complete=True)
        if plan.blocked:
            self.notice(blockage_line(plan))
        removal = [task.name for task in plan.tasks if task.removal is not None]
        if removal:
            message = ("Would remove" if preview or plan.blocked else "Will remove")
            message += " previous retained results for Tasks: " + ", ".join(removal)
            message += (" if the blocked plan can proceed." if plan.blocked else
                        " when this plan is run." if preview else " before submitting replacement work.")
            self.notice(message)
        table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
        table.add_column("Task", ratio=2, min_width=12)
        table.add_column("Next action", width=11, no_wrap=True)
        table.add_column("Why", ratio=3)
        if self.plain or self.console.width < 64:
            self.line("Task / Next action / Why" if self.terminal else
                      f"{'Task':<33} {'Next action':<14} Why", complete=True)
        for row in rows:
            task = row.task
            action = NEXT_ACTIONS[task.action]
            reason = plan_reason(task)
            if self.terminal and (self.plain or self.console.width < 64):
                self.line("Task " + task.name)
                self.line("  " + action, complete=True, style="bold cyan")
                self.line("  " + reason, complete=task.action == "deferred")
            elif self.plain:
                self.line(f"{'Task ' + task.name:<33} {action:<14} {reason}")
            else:
                table.add_row(self.text(task.name), Text(action, style="bold cyan"),
                              self.text(reason, complete=task.action == "deferred"))
        if not self.plain and self.console.width >= 64:
            self.console.print(table)
        if expand:
            for row in rows:
                self.details(row)
                if not plan.blocked:
                    for local in row.task.pending:
                        self.line(f"  Would submit {row.task.name}__{local}")

    def details(self, row):
        task = row.task
        self.line(f"Details for Task {task.name}:")
        self.line(f"  State: {row.state}; Jobs completed: {row.progress}", complete=True)
        names = lifecycle_jobs(ordered_targets(task.structure))
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column(ratio=2, min_width=12)
        table.add_column(width=12, no_wrap=True)
        for local in names:
            name = _LIFECYCLE_NAMES.get(local, local)
            state = row.jobs[local]
            style = "dim" if local in _LIFECYCLE_NAMES else ""
            if self.terminal and self.console.width < 48:
                self.line("  " + name, style=style)
                if self.plain:
                    self.line("    " + state, complete=True)
                else:
                    self.console.print(Text("    ") + self.state(state), no_wrap=False, overflow="fold")
            elif self.plain:
                width = max(1, self.console.width - 16) if self.terminal else 28
                label = Text(name)
                if self.truncate and label.cell_len > width:
                    label.truncate(width if self.unicode else width - 3,
                                   overflow="ellipsis" if self.unicode else "crop")
                    if not self.unicode:
                        label.append("...")
                self.line(f"  {label.plain:<{width}}  {state}", complete=True)
            else:
                table.add_row(self.text(name, style=style), self.state(state))
        if not self.plain and self.console.width >= 48:
            self.console.print(table)
        for line in (f"Condition: {condition(task)}", f"Next action: {NEXT_ACTIONS[task.action]}", f"Reason: {task.reason}",
                     *task_details(self.store, task)):
            self.line("  " + line)


def output_options(command):
    for name, help_text in (
        ("details", "Expand whole Tasks with lifecycle jobs and diagnostics."),
        ("plain", "Use undecorated output."),
        ("no_truncate", "Show full names and reasons, wrapping in terminals."),
    ):
        if not isinstance(command, click.Command) or not any(parameter.name == name for parameter in command.params):
            command = click.option("--" + name.replace("_", "-"), is_flag=True, help=help_text)(command)
    return command
