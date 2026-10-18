"""Keyboard status browsing over a snapshot acquired by an owned process."""

from collections import Counter
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import cast

import click
from rich.text import Text
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.filter import Monochrome, NoColor
from textual.screen import Screen
from textual.widgets import Button, Collapsible, OptionList, Static
from textual.widgets._collapsible import CollapsibleTitle

from .diagnostics import error_message
from .presentation import ACTIVE, _VISUALS, progress_text


def state_text(state):
    symbol, style = _VISUALS[state]
    return Text(f"{symbol} {state}", style=style)


def middle_name(value, width):
    if cell_len(value) <= width:
        return value
    if width < 2:
        return "…"[:width]
    left = Text(value)
    left.truncate((width - 1) // 2, overflow="crop")
    available, suffix = width - left.cell_len - 1, []
    for char in reversed(value):
        if cell_len(char) > available:
            break
        suffix.append(char)
        available -= cell_len(char)
    return left.plain + "…" + "".join(reversed(suffix))


class Breadcrumb:
    def __init__(self, parts):
        self.parts = parts

    def __rich_console__(self, console, options):
        yield Text(middle_name(" > ".join(self.parts), options.max_width), no_wrap=True)


class Shortcuts:
    def __init__(self, primary="↑↓ select  Enter open  Esc back", secondary="Tab controls  g groups  ? help  q quit"):
        self.primary, self.secondary = primary, secondary

    def __rich_console__(self, console, options):
        separator = "  " if len(self.primary) + len(self.secondary) + 2 <= options.max_width else "\n"
        yield Text(self.primary + separator + self.secondary)


class Scope:
    def __init__(self, shown, selected, total, selectors, states):
        self.shown, self.selected, self.total = shown, selected, total
        self.selectors, self.states = selectors, states

    def __rich_console__(self, console, options):
        width = options.max_width
        if width < 60:
            yield Text(f"Showing {self.shown}/{self.selected} selected; workflow {self.total}\n"
                       f"Scope: {middle_name(self.selectors, width - 7)}\n"
                       f"State: {middle_name(self.states, width - 7)}")
        else:
            states = middle_name(self.states, max(8, width // 3))
            selectors = middle_name(self.selectors, width - len(states) - 16)
            yield Text(f"Showing {self.shown} of {self.selected} selected Tasks ({self.total} in workflow)\n"
                       f"Scope: {selectors}; State: {states}")


class GroupPrompt:
    def __init__(self, label, counts, total, matching):
        self.label, self.counts, self.total, self.matching = label, counts, total, matching

    def __rich_console__(self, console, options):
        line = Text(middle_name(self.label, options.max_width) + "\n")
        width = max(3, min(18, options.max_width - len(f"{self.counts['reusable']}/{self.total}") - 5))
        line.append("✓ " + progress_text(self.counts['reusable'], self.total, width), "green")
        line.append("\n" if options.max_width < 60 else "  ")
        for state, count in self.counts.items():
            line.append_text(state_text(state))
            line.append(f" {count}  ")
        if self.matching is not None:
            line.append(f"\n{self.matching} Task{'s match' if self.matching != 1 else ' matches'} the filter")
        yield line


class TaskPrompt:
    def __init__(self, task):
        self.task = task

    def __rich_console__(self, console, options):
        task = self.task
        state = state_text(task["state"])
        fraction = f"{task['completed']}/{task['total']}"
        width = max(3, min(18, options.max_width - state.cell_len - len(fraction) - 5))
        line = Text(middle_name(task["name"], options.max_width) + "\n")
        line.append_text(state)
        line.append("  " + progress_text(task["completed"], task["total"], width))
        if task["detail"]:
            line.append("\n" + task["detail"])
        yield line


class JobPrompt:
    def __init__(self, job):
        self.job = job

    def __rich_console__(self, console, options):
        state = state_text(self.job["observed_state"])
        if self.job["requires_repetition"]:
            state.append("; repeat required")
        name = self.job["local"]
        if cell_len(name) + state.cell_len + 2 <= options.max_width:
            yield Text(name + "  ") + state
        else:
            yield Text(middle_name(name, options.max_width) + "\n") + state


class BrowserView(Screen):
    BINDINGS = [Binding("escape", "app.back", show=False)]

    def __init__(self, breadcrumb):
        super().__init__()
        self.breadcrumb = breadcrumb

    @property
    def snapshot(self) -> dict:
        snapshot = cast(StatusBrowser, self.app).snapshot
        assert snapshot is not None
        return snapshot

    def heading(self):
        return Static(Breadcrumb(self.breadcrumb), classes="breadcrumb")

    def scope(self):
        browser = cast(StatusBrowser, self.app)
        shown = sum(browser.matches(task) for task in self.snapshot["tasks"])
        selectors = ", ".join(browser.selectors) or "all Tasks"
        states = ", ".join(browser.statuses) or "all"
        return Static(Scope(shown, len(self.snapshot["tasks"]), self.snapshot["total"], selectors, states))

    def footer(self):
        return Static(Shortcuts(), classes="shortcuts")

    def notices(self):
        if self.snapshot.get("notices"):
            with VerticalScroll(classes="notice-pane"):
                yield Static(Text(self.snapshot["notices"], style="yellow"))


class GroupsView(BrowserView):
    def __init__(self):
        super().__init__(["Groups"])
        self.groups = []

    def compose(self) -> ComposeResult:
        yield self.heading()
        yield self.scope()
        yield from self.notices()
        options = []
        tasks = {task["name"]: task for task in self.snapshot["tasks"]}
        for group in self.snapshot["groups"]:
            members = [tasks[name] for name in group["members"]]
            matching = sum(cast(StatusBrowser, self.app).matches(task) for task in members)
            if not matching:
                continue
            self.groups.append(group)
            counts = Counter("active" if task["state"] in ACTIVE else task["state"] for task in members)
            options.append(GroupPrompt(group["label"], counts, len(members),
                                       matching if cast(StatusBrowser, self.app).statuses else None))
        yield OptionList(*options, classes="listing")
        yield self.footer()

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        group = self.groups[event.option_index]
        self.app.push_screen(TasksView(group["members"], [*self.breadcrumb, group["label"]], group["label"]))


class TasksView(BrowserView):
    def __init__(self, members=None, breadcrumb=None, group=None, *, expanded=False):
        super().__init__(breadcrumb or ["Tasks"])
        self.members, self.group, self.expanded = members, group, expanded
        self.items = []

    def compose(self) -> ComposeResult:
        yield self.heading()
        yield self.scope()
        yield from self.notices()
        yield Static(Breadcrumb(["Tasks in " + self.group if self.group else "Selected Tasks"]))
        options: list[TaskPrompt | JobPrompt] = []
        for task in self.snapshot["tasks"]:
            if not cast(StatusBrowser, self.app).matches(task):
                continue
            if self.members is not None and task["name"] not in self.members:
                continue
            options.append(TaskPrompt(task))
            self.items.append((task, None))
            if self.expanded:
                for job in task["jobs"]:
                    options.append(JobPrompt(job))
                    self.items.append((task, job))
        yield OptionList(*options, classes="listing")
        yield self.footer()

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        task, job = self.items[event.option_index]
        breadcrumb = [*self.breadcrumb, task["name"]]
        self.app.push_screen(TaskView(task, breadcrumb) if job is None else JobView(job, [*breadcrumb, job["local"]]))


class TaskView(BrowserView):
    def __init__(self, task, breadcrumb):
        super().__init__(breadcrumb)
        self.observation = task
        self.copy_commands = dict(zip(("copy-preview", "copy-run", "copy-cancel"), task["commands"]))

    def compose(self) -> ComposeResult:
        task = self.observation
        yield self.heading()
        yield from self.notices()
        with VerticalScroll():
            lead = Text("State: ") + state_text(task["state"])
            lead.append(f"\nJobs completed: {progress_text(task['completed'], task['total'])}"
                        f"\nNext action: {task['next_action']}\nReason: {task['reason']}")
            yield Static(lead)
            yield OptionList(*(JobPrompt(job) for job in task["jobs"]), classes="jobs")
            yield Static("Task: " + task["name"], markup=False)
            with Collapsible(title="Attempt, paths, and diagnostics"):
                yield Static(task["diagnostics"], markup=False)
            yield Static("Ordinary commands (run in another terminal):")
            for identifier, label in (("copy-preview", "Copy run preview"), ("copy-run", "Copy run command"),
                                      ("copy-cancel", "Copy cancellation command")):
                yield Static(self.copy_commands[identifier], markup=False)
                yield Button(label, id=identifier)
        yield self.footer()

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        job = self.observation["jobs"][event.option_index]
        self.app.push_screen(JobView(job, [*self.breadcrumb, job["local"]]))

    def on_button_pressed(self, event):
        if event.button.id in self.copy_commands:
            self.app.copy_to_clipboard(self.copy_commands[event.button.id])
            event.stop()


class JobView(BrowserView):
    def __init__(self, job, breadcrumb):
        super().__init__(breadcrumb)
        self.job = job

    def compose(self) -> ComposeResult:
        job = self.job
        yield self.heading()
        yield from self.notices()
        with VerticalScroll():
            yield Static(Text("Observed state: ") + state_text(job["observed_state"]))
            if job["requires_repetition"]:
                yield Static("Requires repetition for Task progress.")
            yield Static(f"Public job: {job['name']}\nSubmission: {job['submission'] or 'not submitted'}\n"
                         f"Backend job: {job['backend_id'] if job['backend_id'] is not None else 'unavailable'}", markup=False)
            for stream, path in job["logs"].items():
                yield Static(f"Log {stream}: {path}", markup=False)
        yield self.footer()

    def footer(self):
        return Static(Shortcuts("↑↓ scroll  PgUp/PgDn page", "Esc back  g groups  ? help  q quit"), classes="shortcuts")


class HelpView(BrowserView):
    def __init__(self):
        super().__init__(["Keyboard reference"])

    def compose(self) -> ComposeResult:
        yield self.heading()
        with VerticalScroll():
            yield Static("Up/Down: select a row\nEnter: open a row or activate a control\n"
                         "Esc: close help or go back; stay at the root\n"
                         "Home/End: first/last row or beginning/end of details\nPage Up/Page Down: page through rows or details\n"
                         "Tab/Shift+Tab: move between controls and expandable sections\n"
                         "g: grouped summary within the original selection\n?: keyboard reference\nq: quit\n"
                         "Ctrl+C: interrupt\n\nBrowsing uses captured observations. No commands are executed here.")
            yield Static("Original Task selectors: " + ", ".join(cast(StatusBrowser, self.app).selectors or ("all Tasks",)),
                         markup=False)
            yield Static("Task state filter: " + ", ".join(cast(StatusBrowser, self.app).statuses or ("all",)),
                         markup=False)
            with Collapsible(title="Inspection output and diagnostics"):
                yield Static(Text.from_ansi(self.snapshot.get("inspection_output", "") + "\n" +
                                           self.snapshot.get("inspection_diagnostics", "")))
        yield self.footer()

    def footer(self):
        return Static(Shortcuts("↑↓ scroll  Tab controls  Enter expand", "Esc close  g groups  q quit"), classes="shortcuts")


class ErrorView(BrowserView):
    def __init__(self, failure, output):
        super().__init__(["No snapshot available"])
        self.failure, self.output = failure, output

    def compose(self) -> ComposeResult:
        yield self.heading()
        summary = self.failure.partition("Error: ")[2] or self.failure
        summary = summary.partition("Traceback:")[0].strip()
        with VerticalScroll():
            yield Static(summary, markup=False)
            with Collapsible(title="Error details"):
                yield Static(Text.from_ansi(self.failure))
            if self.output:
                with Collapsible(title="Captured workflow output"):
                    yield Static(Text.from_ansi(self.output))
        yield Static("Tab: controls  Enter: expand details  q: quit", classes="shortcuts")

    def on_mount(self):
        self.query_one(CollapsibleTitle).focus()


class ResizeView(Screen):
    def compose(self) -> ComposeResult:
        yield Static("Terminal too small. Resize to at least 40 × 12. q: quit", markup=False)


class Inspection:
    """Own one acquisition and all subprocesses it creates, never workflow jobs."""

    def __init__(self, arguments):
        self.directory = tempfile.TemporaryDirectory(prefix="gwflow-status-")
        self.root = Path(self.directory.name)
        self.result = self.root / "snapshot.json"
        self.started = time.monotonic()
        with (self.root / "stdout").open("wb") as output, (self.root / "stderr").open("wb") as errors:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "gwflow._inspection_process", *arguments[0],
                 "status", "--snapshot-file", str(self.result), "--", *arguments[1]],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True,
            )

    def phase(self):
        with (self.root / "stderr").open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 8192))
            lines = stream.read().decode(errors="replace").splitlines()
        return next((line for line in reversed(lines) if line.startswith(
            ("Inspecting ", "Waiting ", "Pulling image:"))), "Loading workflow...")

    def completed(self):
        return self.process.poll() is not None

    def outcome(self):
        output = (self.root / "stdout").read_text(errors="replace")
        diagnostics = (self.root / "stderr").read_text(errors="replace")
        if self.process.returncode:
            return None, diagnostics or "Inspection failed without diagnostics.", output
        snapshot = json.loads(self.result.read_text())
        snapshot["inspection_output"] = output
        snapshot["inspection_diagnostics"] = diagnostics
        return snapshot, "", output

    def close(self):
        try:
            self.process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()
        self.directory.cleanup()


class StatusBrowser(App[int]):
    CSS = """
    Screen { layout: vertical; }
    #breadcrumb, .breadcrumb { height: auto; text-style: bold; color: cyan; }
    #activity { height: auto; }
    #rows, .listing { height: 1fr; border: none; }
    .jobs { height: 8; min-height: 3; max-height: 50%; border: none; }
    .notice-pane { height: auto; min-height: 2; max-height: 25%; border-left: wide yellow; }
    #shortcuts, .shortcuts { height: auto; dock: bottom; }
    """
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("ctrl+c", "interrupt", show=False, priority=True),
                Binding("g", "groups", "Groups"), Binding("question_mark", "help", "Help")]

    def __init__(self, arguments, *, selectors=(), statuses=(), details=False):
        super().__init__()
        self.no_color = False
        self.arguments = arguments
        self.selectors, self.statuses, self.details = selectors, statuses, details
        self.inspection = None
        self.snapshot: dict | None = None
        self.failure = ""
        self.captured_output = ""
        self.interrupted = False
        self.too_small = False

    def get_line_filters(self):
        return [item for item in super().get_line_filters() if not isinstance(item, (NoColor, Monochrome))]

    def matches(self, task):
        return not self.statuses or task["state"] in self.statuses

    def compose(self) -> ComposeResult:
        yield Static("Groups", id="breadcrumb")
        yield Static("Loading workflow...", id="activity")
        yield OptionList(id="rows")
        yield Static("q: quit", id="shortcuts")

    def on_mount(self):
        self.inspection = Inspection(self.arguments)
        self.set_interval(0.1, self.inspect_progress)

    def on_resize(self, event):
        too_small = event.size.width < 40 or event.size.height < 12
        if too_small == self.too_small:
            return
        self.too_small = too_small
        if too_small:
            self.push_screen(ResizeView())
        elif isinstance(self.screen, ResizeView):
            self.pop_screen()

    def show_snapshot_view(self, view):
        if isinstance(self.screen, ResizeView):
            self.pop_screen()
        self.push_screen(view)
        if self.too_small:
            self.push_screen(ResizeView())

    def inspect_progress(self):
        if self.inspection is None:
            return
        elapsed = int(time.monotonic() - self.inspection.started)
        self.screen_stack[0].query_one("#activity", Static).update(f"{self.inspection.phase()}  {elapsed}s elapsed")
        if not self.inspection.completed():
            return
        try:
            self.snapshot, self.failure, self.captured_output = self.inspection.outcome()
        finally:
            self.inspection.close()
            self.inspection = None
        if self.snapshot is None:
            self.show_snapshot_view(ErrorView(self.failure, self.captured_output))
            return
        self.show_snapshot_view(TasksView(expanded=self.details) if self.selectors or self.details else GroupsView())

    def action_back(self):
        if not self.too_small and len(self.screen_stack) > 2:
            self.pop_screen()

    def action_groups(self):
        if not self.too_small and self.snapshot is not None:
            while len(self.screen_stack) > 2:
                self.pop_screen()
            self.switch_screen(GroupsView())

    def action_help(self):
        if not self.too_small and self.snapshot is not None and not isinstance(self.screen, HelpView):
            self.push_screen(HelpView())

    def action_quit(self):
        self.exit(0 if self.snapshot is not None else 1)

    def action_interrupt(self):
        self.interrupted = True
        self.action_quit()

    def close_inspection(self):
        if self.inspection is not None:
            self.inspection.close()
            self.inspection = None

    def _handle_exception(self, error):
        self.failure = error_message(error, "Interactive status")
        super()._handle_exception(error)


def run_browser(ctx, selectors, statuses, details):
    options = ["--file", f"{ctx.workflow_file}:{ctx.workflow_obj}", "--backend", ctx.backend]
    root = click.get_current_context().find_root()
    if root.get_parameter_source("verbose") == click.core.ParameterSource.COMMANDLINE:
        options.extend(("--verbose", root.params["verbose"]))
    arguments = options, selectors
    app = StatusBrowser(arguments, selectors=selectors, statuses=statuses, details=details)
    try:
        result = app.run()
    finally:
        app.close_inspection()
    if app.interrupted:
        raise KeyboardInterrupt
    if app.failure:
        click.echo(app.failure, err=True, color=False)
    return app.return_code or result or 0
