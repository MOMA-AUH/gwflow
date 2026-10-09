"""Static Task views derived from planning observations, without backend access."""

from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
import shlex
import sys
import textwrap

import click
from gwf.backends import BackendStatus
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .inspection import blockage_line, condition, task_details
from .lifecycle import TaskObservation, ordered_targets, producer_names
from .workflow import lifecycle_jobs


STATES = ("pending", "queued", "preparing", "running", "finishing", "reusable",
          "repairable", "deferred", "failed", "canceled", "blocked")
ACTIVE = ("preparing", "running", "finishing")
ATTENTION = ("blocked", "failed", "canceled")
DIAGNOSTIC_JOBS = ("failed", "canceled", "running", "queued")
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


@dataclass
class TaskGroup:
    prefix: str
    factory: str
    label: str
    members: list[str]

    @property
    def qualified(self):
        return f"{self.prefix}@{self.factory}"


def task_groups(workflow, plan):
    """Resolve labels and group order from the entire current declaration."""
    groups = {}
    for task in task_order(workflow, plan):
        naming = workflow._task_naming[task.name]
        key = naming.factory, naming.prefix
        if key not in groups:
            groups[key] = TaskGroup(naming.prefix, naming.factory, naming.prefix, [])
        groups[key].members.append(task.name)
    prefixes = Counter(group.prefix for group in groups.values())
    for group in groups.values():
        if prefixes[group.prefix] > 1:
            group.label = group.qualified
    return list(groups.values())


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
        detail.append(blocking_detail(task.reason))
    elif task.action == "reuse":
        detail.append("work present" if task.work_present else "work cleaned")
    elif task.action == "repair":
        detail.append("retained results need repair")
    elif task.action == "transfer":
        detail.append("results transfer needs recovery")
    if task.action == "deferred":
        detail.append(required_followup(task))
    if task.restart_required and task.attempt:
        detail.append("fresh attempt required")
    if task.action in ("retry", "prepare"):
        detail.append("retry available")
    detail.extend(job_diagnostics(counts, state))
    if completed is None:
        detail.append("progress unavailable")
    return TaskRow(task, state, completed, len(jobs), "; ".join(detail), jobs)


def blocking_reason(reason):
    if reason.startswith("Target ") and ": image unavailable for " in reason:
        return "image unavailable"
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
    return None


def blocking_detail(reason):
    return blocking_reason(reason) or "validation prevents proceeding; see gwf explain --details"


def job_diagnostics(counts, state):
    for label in DIAGNOSTIC_JOBS:
        count = counts[label]
        if count:
            description = "still running" if label == "running" and state in ATTENTION else label
            yield f"{count} job{'s' if count != 1 else ''} {description}"


def needs_later_finish(task):
    finishing = task.submissions.get("gwflow_complete")
    return task.action == "retry" and finishing is not None and finishing.state == "active"


def required_followup(task):
    if task.action == "deferred":
        return "run again after upstream result recovery"
    if needs_later_finish(task):
        return "run again after the queued completion job settles"
    return None


def plan_reason(task):
    """Keep the treatment legible; full planner diagnostics remain in details."""
    if task.action == "blocked":
        concise = blocking_reason(task.reason)
        if concise == "image unavailable":
            return "image unavailable; check image access"
        if concise:
            return concise + "; resolve before proceeding"
        return "validation prevents proceeding; see --details"
    if task.action == "fresh":
        return task.reason.split(";", 1)[0].split(":", 1)[0] + "; fresh attempt required"
    if task.action == "retry":
        reason = "repeat failed, canceled, or invalid jobs; retain valid completed work"
        if needs_later_finish(task):
            reason += "; " + required_followup(task)
        return reason
    return {
        "initialize": "resume selected initialization before computation",
        "continue": "submit remaining jobs without repeating admitted work",
        "prepare": "preparation interrupted; restart in the same attempt",
        "repair": "retained results missing or changed; restore from valid work",
        "deferred": required_followup(task),
        "active": "jobs queued or running; no new submission needed now",
    }.get(task.action, task.reason)


def summary(rows, total):
    counts = Counter(row.state for row in rows)
    return f"{len(rows)} of {total} Tasks selected" + (
        "\n" + ", ".join(f"{value} {state}" for state, value in counts.items()) if counts else "")


def overview_counts(rows):
    return Counter("active" if row.state in ACTIVE else row.state for row in rows)


def observed_jobs(task):
    """Count observations, including removed jobs, independently of progress."""
    counts = Counter()
    for item in task.submissions.values():
        if item.state == "active":
            counts["queued" if item.backend_state == BackendStatus.SUBMITTED else "running"] += 1
        elif item.state in ("failed", "cancelled"):
            counts["canceled" if item.state == "cancelled" else "failed"] += 1
    incomplete = not task.observations_available or any(
        item.state == "uncertain" for item in task.submissions.values())
    return counts, incomplete


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
    "active": ("▶", "blue"),
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

    def status_line(self, value, *, style=""):
        """Wrap prose at whitespace; leave identifiers intact for the terminal."""
        if not self.terminal:
            click.echo(value, color=False)
            return
        for paragraph in value.split("\n"):
            indent = paragraph[:len(paragraph) - len(paragraph.lstrip())]
            lines = textwrap.wrap(paragraph, width=self.console.width, subsequent_indent=indent,
                                  break_long_words=False, break_on_hyphens=False) or [""]
            rendered = "\n".join(lines)
            if not self.unicode:
                rendered = rendered.encode(self.console.encoding, errors="backslashreplace").decode(self.console.encoding)
            self.console.print(Text(rendered, style=style if not self.plain else ""), soft_wrap=True)

    def state(self, name):
        symbol, style = _VISUALS[name]
        return Text(f"{symbol} {name}", style=style, no_wrap=True)

    def _state_counts(self, counts):
        labels = []
        for state, count in counts.items():
            label = f"{count} {state}"
            if self.plain:
                labels.append(Text(label))
            else:
                symbol, style = _VISUALS[state]
                labels.append(Text(f"{symbol} {label}", style=style))
        return Text(", ").join(labels)

    def overview(self, groups, rows, total):
        def render(text):
            if self.plain:
                self.status_line(text.plain)
            else:
                self.console.print(text)

        self.status_line(f"{len(rows)} of {total} Tasks selected", style="bold cyan")
        counts = overview_counts(rows)
        render(self._state_counts(counts))
        selected = {row.task.name: row for row in rows}
        visible = [(group, [selected[name] for name in group.members if name in selected]) for group in groups]
        visible = [(group, members) for group, members in visible if members]
        if not visible:
            self.status_line("No Tasks selected.")
            return
        width = max(5, *(Text(group.label).cell_len for group, _ in visible))
        self.status_line("")
        entries = []
        for group, members in visible:
            counts = overview_counts(members)
            fraction = Text(f"{counts['reusable']}/{len(members)}")
            if not self.plain:
                symbol, style = _VISUALS["reusable"]
                fraction = Text(f"{symbol} {fraction.plain}", style=style if counts["reusable"] else "dim")
            other = self._state_counts({state: count for state, count in counts.items() if state != "reusable"})
            entries.append((group.label, fraction, other))
        fraction_width = max(8, *(fraction.cell_len for _, fraction, _ in entries))
        heading = f"{'Group':<{width}}  {'Reusable':>{fraction_width}}  Other states"
        lines = []
        for label, fraction, other in entries:
            line = Text(label + " " * (width - Text(label).cell_len + 2 + fraction_width - fraction.cell_len))
            line += fraction
            if other.plain:
                line += Text("  ") + other
            lines.append(line)
        if not self.terminal or max(Text(heading).cell_len, *(line.cell_len for line in lines)) <= self.console.width:
            self.status_line(heading, style="bold")
            for line in lines:
                render(line)
        else:
            for index, (label, fraction, other) in enumerate(entries):
                if index:
                    self.status_line("")
                self.status_line(label, style="bold")
                render(Text("  Reusable: ") + fraction)
                if other.plain:
                    render(Text("  Other states: ") + other)

    def attention(self, rows, command):
        categories = [(state, [row for row in rows if row.state == state])
                      for state in ATTENTION]
        if not any(members for _, members in categories):
            return
        self.status_line("\nNeeds attention", style="bold yellow")
        for state, members in categories:
            if not members:
                continue
            observations = [(row, *observed_jobs(row.task)) for row in members]
            counts = Counter()
            for _, jobs, _ in observations:
                counts.update(jobs)
            incomplete = sum(unavailable for _, _, unavailable in observations)
            count = len(members)
            heading = f"{state.title()}: {count} Task{'s' if count != 1 else ''}"
            if count > 5:
                heading += f" (5 shown, {count - 5} omitted)"
            self.status_line(heading)
            if counts:
                self.status_line("  Jobs: " + ", ".join(f"{counts[label]} {label}" for label in DIAGNOSTIC_JOBS if counts[label]))
            if incomplete:
                self.status_line(f"  Observations incomplete for {incomplete} Task{'s' if incomplete != 1 else ''}; "
                          "job totals include only available observations.")
            for row, jobs, unavailable in observations[:5]:
                detail = []
                if state == "blocked":
                    detail.append(blocking_detail(row.task.reason))
                detail.extend(job_diagnostics(jobs, state))
                if unavailable:
                    detail.append("observations incomplete")
                if row.completed is None:
                    detail.append("progress unavailable")
                description = "; ".join(detail)
                line = f"  {row.task.name}  {description}"
                if self.terminal and Text(line).cell_len > self.console.width:
                    self.status_line("  " + row.task.name)
                    self.status_line("    " + description)
                else:
                    self.status_line(line)
            self.status_line(f"\nAll {state} Tasks:")
            # Let the terminal wrap visually without inserting breaks into a
            # command that users can copy and execute.
            click.echo("  " + shlex.join([*command, "--status", state, "--instances"]), color=False)
            self.status_line("")

    def workflow_notices(self, tasks, rows):
        tasks = list(tasks)
        blocked = [task for task in tasks if task.action == "blocked"]
        followups = [(task, reminder) for task in tasks if (reminder := required_followup(task))]
        if not blocked and not followups:
            return
        selected = {row.task.name for row in rows}

        def label(task):
            return task.name + (" (outside selection)" if task.name not in selected else "")

        self.status_line("")
        self.status_line("Workflow notices", style="bold yellow")
        if blocked:
            self.status_line("  Workflow blocked — no new jobs will be submitted. Blocked Tasks:")
            for task in blocked:
                line = "    " + label(task)
                if self.terminal and task.name not in selected and Text(line).cell_len > self.console.width:
                    self.status_line("    " + task.name)
                    self.status_line("      (outside selection)")
                else:
                    self.status_line(line)
            self.status_line("  Already active jobs may still be running.")
        for task, reminder in followups:
            line = f"  Task {label(task)}: {reminder}"
            if self.terminal and Text(line).cell_len > self.console.width:
                self.status_line("  Task " + task.name + (":" if task.name in selected else ""))
                if task.name not in selected:
                    self.status_line("    (outside selection):")
                self.status_line("    " + reminder)
            else:
                self.status_line(line)

    def status(self, rows, total, *, expand=False):
        self.status_line(summary(rows, total), style="bold cyan")
        if not rows:
            self.status_line("No Tasks selected.")
            return
        headers = ["Task", *(["Target"] if expand else []), "State", "Jobs completed", "Detail"]
        entries = []
        for row in rows:
            cells = ["Task " + row.task.name, *([""] if expand else []), row.state, row.progress, row.detail]
            entries.append((cells, row.state))
            if expand:
                for name, state in self._status_jobs(row):
                    entries.append((["", name, state, "", ""], state))
        widths = [max(Text(cells[index]).cell_len for cells in [headers, *(cells for cells, _ in entries)])
                  for index in range(len(headers))]
        if not self.terminal or sum(widths) + 2 * (len(widths) - 1) <= self.console.width:
            def line(cells):
                return "  ".join(value + " " * (width - Text(value).cell_len)
                                 for value, width in zip(cells, widths)).rstrip()
            self.status_line(line(headers), style="bold")
            for cells, state in entries:
                self.status_line(line(cells), style=_VISUALS[state][1])
            return
        for index, row in enumerate(rows):
            if index:
                self.status_line("")
            self.status_line("Task " + row.task.name, style="bold")
            self.status_line("  State: " + row.state, style=_VISUALS[row.state][1])
            self.status_line("  Jobs completed: " + row.progress)
            if row.detail:
                self.status_line("  " + row.detail)
            if expand:
                for name, state in self._status_jobs(row):
                    self.status_line("  " + name)
                    self.status_line("    State: " + state, style=_VISUALS[state][1])

    def _status_jobs(self, row):
        names = lifecycle_jobs(ordered_targets(row.task.structure))
        for index, local in enumerate(names):
            last = index == len(names) - 1
            branch = ("`- " if last else "|- ") if self.plain else ("└─ " if last else "├─ ")
            label = f" {_LIFECYCLE_NAMES[local]}" if local in _LIFECYCLE_NAMES else ""
            yield branch + f"{row.task.name}__{local}" + label, row.jobs[local]

    def plan(self, plan, rows, *, expand=False, preview=True):
        self.line("Managed whole-workflow plan", complete=True)
        if plan.blocked:
            self.notice(blockage_line(plan))
        removal = [task.name for task in plan.tasks if task.removal is not None]
        if removal:
            message = ("Would remove" if preview or plan.blocked else "Will remove")
            message += " previous retained results for Tasks: " + ", ".join(removal)
            message += (" if the blocked plan can proceed." if plan.blocked else
                        " when this plan is run." if preview else " during execution of this plan.")
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
            complete = task.action == "deferred" or needs_later_finish(task)
            if self.terminal and (self.plain or self.console.width < 64):
                self.line("Task " + task.name)
                self.line("  " + action, complete=True, style="bold cyan")
                self.line("  " + reason, complete=complete)
            elif self.plain:
                self.line(f"{'Task ' + task.name:<33} {action:<14} {reason}")
            else:
                table.add_row(self.text(task.name), Text(action, style="bold cyan"),
                              self.text(reason, complete=complete))
        if not self.plain and self.console.width >= 64:
            self.console.print(table)
        if expand:
            for row in rows:
                self.details(row)
                if not plan.blocked:
                    for local in row.task.pending:
                        self.line(f"  Would submit {row.task.name}__{local}")

    def submissions(self, outcomes, *, interrupted=False, details=False):
        confirmed = [outcome for outcome in outcomes if outcome.confirmed]
        jobs, tasks = len(confirmed), len({outcome.task for outcome in confirmed})
        if interrupted:
            unknown = sum(outcome.entered and not outcome.confirmed for outcome in outcomes)
            unattempted = sum(not outcome.entered for outcome in outcomes)
            self.notice(f"Submission interrupted: {jobs} job{'s' if jobs != 1 else ''} confirmed submitted "
                        f"across {tasks} Task{'s' if tasks != 1 else ''}; "
                        f"{unknown} submission outcome{'s' if unknown != 1 else ''} unknown; "
                        f"{unattempted} planned job{'s' if unattempted != 1 else ''} not attempted. "
                        "Already submitted jobs may continue; submission was not rolled back.")
        elif confirmed:
            self.line(f"Submitted {jobs} job{'s' if jobs != 1 else ''} across {tasks} Task{'s' if tasks != 1 else ''}.",
                      complete=True, style="bold green")
        else:
            self.line("No new jobs were submitted.", complete=True)
        if details and outcomes:
            self.line("Submission outcomes:", complete=True)
            for outcome in outcomes:
                state = ("confirmed submitted" if outcome.confirmed else
                         "outcome unknown" if outcome.entered else "not attempted")
                self.line(f"  {outcome.task}__{outcome.local}: {state}")
                if outcome.submission:
                    self.line("    Submission: " + outcome.submission)
                if outcome.job_id is not None:
                    self.line(f"    Backend job: {outcome.job_id}")

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
