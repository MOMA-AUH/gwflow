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
from rich.spinner import Spinner
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.filter import Monochrome, NoColor
from textual.screen import Screen
from textual.widgets import Collapsible, DataTable, Input, OptionList, SelectionList, Static
from textual.widgets._collapsible import CollapsibleTitle
from textual.widgets.option_list import Option

from .diagnostics import error_message
from .presentation import ACTIVE, STATES, _VISUALS, format_reason, progress_display


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
    """Fit complete key–action pairs on one line, in priority order."""

    def __init__(self, *actions, back=None, show_help=True, show_quit=True):
        self.actions = actions
        self.trailing = ([('Esc', back)] if back else []) + ([('?', 'Help')] if show_help else [])
        if show_quit:
            self.trailing.append(('q', 'Quit'))

    def __rich_console__(self, console, options):
        def width(items):
            return sum(cell_len(key + ' ' + label) for key, label in items) + max(0, len(items) - 1) * 3

        selected = []
        for action in self.actions:
            if width([*selected, action, *self.trailing]) <= options.max_width:
                selected.append(action)
        result = Text(no_wrap=True, overflow='crop')
        for key, label in [*selected, *self.trailing]:
            if result:
                result.append(' │ ', style='dim')
            result.append(key, style='bold cyan')
            result.append(' ' + label)
        yield result


class Scope:
    def __init__(self, shown, selected, total, selectors, states, name_query):
        self.shown, self.selected, self.total = shown, selected, total
        self.selectors, self.states = selectors, states
        self.name_query = name_query or "none"

    def __rich_console__(self, console, options):
        width = options.max_width
        if width < 60:
            states = middle_name(self.states, max(4, (width - 16) // 2))
            name_query = middle_name(self.name_query, width - cell_len(states) - 16)
            yield Text.assemble(
                f"Showing {self.shown}/{self.selected} selected; workflow {self.total}\n",
                ("Scope:", "bold"), f" {middle_name(self.selectors, width - 7)}\n",
                ("State:", "bold"), f" {states}; ", ("Name:", "bold"), f" {name_query}",
            )
        else:
            states = middle_name(self.states, max(8, width // 4))
            selectors = middle_name(self.selectors, (width - 24) // 3)
            name_query = middle_name(self.name_query, width - cell_len(states) - cell_len(selectors) - 24)
            yield Text.assemble(
                f"Showing {self.shown} of {self.selected} selected Tasks ({self.total} in workflow)\n",
                ("Scope:", "bold"), f" {selectors}; ",
                ("State:", "bold"), f" {states}; ", ("Name:", "bold"), f" {name_query}",
            )


class ProgressColumns:
    """Shared headings and widths for a list of names, progress, and states."""

    def __init__(self, name_width, fraction_width, *, progress_label="Reusable", state_width=None, name_label="Name",
                 detail_width=0):
        self.name_width, self.fraction_width = max(8, name_width), fraction_width
        self.progress_label, self.state_width = progress_label, state_width
        self.state_label = "Other states" if state_width is None else "State"
        self.name_label = name_label
        self.detail_width = max(len("Details"), detail_width) if detail_width else 0

    def bar_width(self, width):
        if self.detail_width and self.state_width is not None and width >= 70:
            name = min(self.name_width, max(12, width // 4))
            available = width - name - self.state_width - self.fraction_width - 9 - min(self.detail_width, 20)
            return max(6, min(14, available))
        return 14

    def inline_detail_width(self, width):
        if not self.detail_width or self.state_width is None or width < 70:
            return 0
        name = min(self.name_width, max(12, width // 4))
        available = width - (self.bar_width(width) + 3 + self.fraction_width) - self.state_width - name - 6
        return min(self.detail_width, available) if available >= len("Details") else 0

    def widths(self, width):
        detail = self.inline_detail_width(width)
        progress = self.bar_width(width) + 3 + self.fraction_width
        if detail:
            return min(self.name_width, width - progress - self.state_width - detail - 6), progress
        available = width * 2 // 5 if self.state_width is None else width - progress - self.state_width - 4
        return min(self.name_width, max(8, available)), progress

    def progress(self, completed, total, *, bar_width=14):
        return progress_display(completed, total, bar_width, fraction_width=self.fraction_width)

    def __rich_console__(self, console, options):
        if options.max_width < 70:
            yield Text(f"{self.name_label} / {self.progress_label} / {self.state_label}", style="bold")
        else:
            name, progress = self.widths(options.max_width)
            tail = f"{self.state_label:<{self.state_width}}  Details" if self.inline_detail_width(options.max_width) else self.state_label
            yield Text(f"{self.name_label:<{name}}  {self.progress_label:<{progress}}  {tail}", style="bold")


class JobColumns:
    def __init__(self, jobs):
        self.name_width = max((cell_len(job['local']) for job in jobs), default=8)
        self.state_width = max((job_state_text(job).cell_len for job in jobs), default=5)

    def width(self, width):
        return max(8, min(self.name_width, width - self.state_width - 2))

    def __rich_console__(self, console, options):
        yield Text(f"{'Name':<{self.width(options.max_width)}}  State", style="bold")


def job_state_text(job):
    state = state_text(job["observed_state"])
    if job["requires_repetition"]:
        state.append("; repeat required")
    return state


class GroupPrompt:
    def __init__(self, label, counts, total, matching, columns):
        self.label, self.counts, self.total, self.matching = label, counts, total, matching
        self.columns = columns

    def __rich_console__(self, console, options):
        states = Text()
        for state, count in self.counts.items():
            if state == "reusable":
                continue
            if states:
                states.append("  ")
            states.append_text(state_text(state))
            states.append(f" {count}")
        name_width, progress_width = self.columns.widths(options.max_width)
        single_line = options.max_width >= 70 and name_width + progress_width + states.cell_len + 4 <= options.max_width
        if single_line:
            name = middle_name(self.label, name_width)
            line = Text(name + " " * (name_width - cell_len(name)) + "  ")
            line.append_text(self.columns.progress(self.counts['reusable'], self.total))
            line.append("  ")
        else:
            line = Text(middle_name(self.label, options.max_width) + "\n")
            width = max(3, min(18, options.max_width - len(f"{self.counts['reusable']}/{self.total}") - 5))
            progress = Text.assemble(("✓ ", "green"))
            progress.append_text(progress_display(self.counts['reusable'], self.total, width))
            line.append_text(progress)
            if states:
                line.append("\n" if progress.cell_len + states.cell_len + 2 > options.max_width else "  ")
        line.append_text(states)
        if self.matching is not None:
            line.append(f"\n{self.matching} Task{'s match' if self.matching != 1 else ' matches'} the filter")
        yield line


def task_summary(detail):
    parts = []
    for part in detail.split('; '):
        words = part.split()
        if len(words) == 3 and words[0].isdigit() and words[1] in ('job', 'jobs'):
            part = f'{words[0]} {words[2]}'
        parts.append(part)
    return ' · '.join(parts)


class TaskPrompt:
    def __init__(self, task, columns, display_name):
        self.task, self.columns = task, columns
        self.display_name = display_name
        self.detail = task_summary(task['detail'])

    def __rich_console__(self, console, options):
        task = self.task
        state = state_text(task["state"])
        fraction = f"{task['completed']}/{task['total']}"
        name_width, progress_width = self.columns.widths(options.max_width)
        detail_indent = 2
        inline_detail = False
        if options.max_width >= 70 and name_width + progress_width + state.cell_len + 4 <= options.max_width:
            name = middle_name(self.display_name, name_width)
            line = Text(name + " " * (name_width - cell_len(name)) + "  ")
            detail_width = self.columns.inline_detail_width(options.max_width)
            line.append_text(self.columns.progress(task['completed'], task['total'],
                                                  bar_width=self.columns.bar_width(options.max_width)))
            line.append("  ")
            line.append_text(state)
            inline_detail = detail_width and cell_len(self.detail) <= detail_width and '\n' not in self.detail
            if inline_detail and self.detail:
                line.append(' ' * (self.columns.state_width - state.cell_len + 2))
                line.append(self.detail)
            detail_indent = name_width + 2
        else:
            width = max(3, min(18, options.max_width - state.cell_len - len(fraction) - 5))
            line = Text(middle_name(self.display_name, options.max_width) + "\n")
            line.append_text(state)
            line.append("  ")
            line.append_text(progress_display(task["completed"], task["total"], width))
        if self.detail and not inline_detail:
            for detail_line in Text(self.detail).wrap(console, max(1, options.max_width - detail_indent)):
                line.append("\n" + " " * detail_indent + detail_line.plain)
        yield line


class JobPrompt:
    def __init__(self, job, columns):
        self.job, self.columns = job, columns

    def __rich_console__(self, console, options):
        state = job_state_text(self.job)
        name = self.job["local"]
        if isinstance(self.columns, ProgressColumns):
            # Expanded jobs share their parent Task's state column.
            name_width, progress_width = self.columns.widths(options.max_width)
            indent, gap = "  ", progress_width + 4
        else:
            name_width = self.columns.width(options.max_width)
            indent, gap = "", 2
        if name_width + gap + state.cell_len <= options.max_width:
            name = indent + middle_name(name, name_width - len(indent))
            yield Text(name + " " * (name_width - cell_len(name) + gap)) + state
        else:
            yield Text(indent + middle_name(name, options.max_width - len(indent)) + "\n") + state


def failure_summary(failure):
    return (failure.partition("Error: ")[2] or failure).partition("Traceback:")[0].strip()


class InspectionStatus(Static):
    """Snapshot age and acquisition outcome, independent of the current view."""

    def update_status(self):
        browser = cast(StatusBrowser, self.app)
        lines = []
        if browser.snapshot is not None:
            age = max(0, int(time.time() - browser.snapshot["finished"]))
            prefix = "Showing previous snapshot;" if browser.failure else "Last inspection finished"
            lines.append(f"{prefix} {age}s ago")
        if browser.inspection is not None:
            elapsed = int(time.monotonic() - browser.inspection.started)
            lines.append(f"Refreshing... {elapsed}s elapsed")
            lines.append(browser.inspection.progress(max(40, self.size.width)))
        if browser.failure:
            summary = " ".join(failure_summary(browser.failure).split())
            lines.append(middle_name("Refresh failed: " + summary, max(40, self.size.width)))
            lines.append("Press Enter for error details" if self.has_focus else "Tab then Enter: error details")
        if browser.navigation_notice:
            lines.append(middle_name(browser.navigation_notice, max(40, self.size.width)))
        self.can_focus = bool(browser.failure)
        value = Text("\n").join(line if isinstance(line, Text) else Text(line) for line in lines)
        if value != getattr(self, "last_value", None):
            self.update(value)
            self.last_value = value

    def on_mount(self):
        self.update_status()

    def on_resize(self):
        self.update_status()

    def on_focus(self):
        # Textual updates has_focus in its base event handler, after this one.
        self.call_later(self.update_status)

    def on_blur(self):
        self.call_later(self.update_status)

    def on_key(self, event):
        browser = cast(StatusBrowser, self.app)
        if event.key == "enter" and browser.failure:
            browser.open_menu(ErrorView(browser.failure, browser.captured_output, previous=True))
            event.stop()


class BrowserView(Screen):
    AUTO_FOCUS = ""
    pinned = False
    BINDINGS = [Binding("escape", "app.back", show=False)]

    def __init__(self, breadcrumb):
        super().__init__()
        self.breadcrumb = breadcrumb
        self.section_states = {}
        self.available = True

    @property
    def snapshot(self) -> dict:
        snapshot = cast(StatusBrowser, self.app).snapshot
        assert snapshot is not None
        return snapshot

    def heading(self):
        return Vertical(Static(Breadcrumb(self.breadcrumb), classes="breadcrumb"), InspectionStatus(), classes="view-heading")

    def scope(self):
        browser = cast(StatusBrowser, self.app)
        shown = sum(browser.matches(task) for task in self.snapshot["tasks"])
        selectors = ", ".join(browser.selectors) or "all Tasks"
        states = ", ".join(browser.statuses) or "all"
        return Static(Scope(shown, len(self.snapshot["tasks"]), self.snapshot["total"], selectors, states,
                            browser.name_query))

    def footer(self):
        return self.shortcut_bar(('Enter', 'Open'), ('↑↓', 'Select'), ('/', 'Search'),
                                 ('f', 'Filter'), ('r', 'Refresh'))

    def shortcut_bar(self, *actions, back='Back', show_help=True, show_quit=True):
        return Static(Shortcuts(*actions, back=back if len(self.app.screen_stack) > 2 else None,
                                show_help=show_help, show_quit=show_quit), classes='shortcuts')

    def listing(self, options, columns, title, *, classes="", list_id=None):
        panel = Vertical(Vertical(Static(columns, classes="list-columns"), classes="list-heading"),
                         OptionList(*options, classes="listing table-list", id=list_id),
                         classes="listing-panel " + classes)
        panel.border_title = title
        return panel

    def notices(self, *, can_focus=True):
        if self.snapshot.get("notices"):
            with VerticalScroll(classes="notice-pane", can_focus=can_focus):
                yield Static(Text(self.snapshot["notices"], style="yellow"))

    def observe_snapshot(self):
        return True

    def selection(self):
        return None, None

    def on_mount(self):
        if self.focused is None and self.focus_chain:
            self.set_focus(self.focus_chain[0])

    def on_resize(self, event):
        self.set_class(event.size.height < 18, "compact")
        # Stacking can make the selected row taller than its previous viewport.
        for listing in self.query(OptionList):
            self.call_after_refresh(listing.scroll_to_highlight)

    def section(self, title):
        return Collapsible(title=title, collapsed=self.section_states.get(title, True))

    def capture_navigation(self):
        focused = self.focused
        return dict(rows=[(listing.highlighted_option.id if listing.highlighted_option else None,
                           listing.highlighted, listing.scroll_offset) for listing in self.query(OptionList)],
                    scrolls=[pane.scroll_offset for pane in self.query(VerticalScroll)],
                    collapsed={section.title: section.collapsed for section in self.query(Collapsible)},
                    focus=(type(focused), focused.id, list(self.query(type(focused))).index(focused))
                    if focused is not None else None)

    def restore_navigation(self, saved):
        for listing, (identifier, index, offset) in zip(self.query(OptionList), saved['rows']):
            indices = {option.id: position for position, option in enumerate(listing.options)}
            selected = indices.get(identifier, min(index or 0, listing.option_count - 1))
            listing.highlighted = selected if listing.option_count else None
            if selected == index:
                listing.scroll_to(offset.x, offset.y, animate=False, immediate=True)
            else:
                listing.scroll_to_highlight()
        if saved['focus'] is not None:
            kind, identifier, index = saved['focus']
            controls = list(self.query(kind))
            focused = next((control for control in controls if identifier and control.id == identifier),
                           controls[index] if index < len(controls) else None)
            if focused is not None:
                self.set_focus(focused, scroll_visible=False)
        for pane, offset in zip(self.query(VerticalScroll), saved['scrolls']):
            pane.scroll_to(offset.x, offset.y, animate=False, immediate=True)

    async def refresh_contents(self, navigation=None):
        navigation = self.capture_navigation() if navigation is None else navigation
        self.section_states = navigation['collapsed']
        # Do not send keys to removed widgets while restoring the saved focus.
        self.set_focus(None, scroll_visible=False)
        await self.recompose()
        self.app.call_after_refresh(self.restore_navigation, navigation)


class GroupsView(BrowserView):
    def __init__(self):
        super().__init__(["Groups"])
        self.groups = []

    def compose(self) -> ComposeResult:
        self.groups = []
        yield self.heading()
        yield self.scope()
        yield from self.notices()
        options = []
        columns = ProgressColumns(max((cell_len(group['label']) for group in self.snapshot['groups']), default=8),
                               max((len(f"{len(group['members'])}/{len(group['members'])}")
                                    for group in self.snapshot['groups']), default=3))
        tasks = {task["name"]: task for task in self.snapshot["tasks"]}
        for group in self.snapshot["groups"]:
            members = [tasks[name] for name in group["members"]]
            matching = sum(cast(StatusBrowser, self.app).matches(task) for task in members)
            if not matching:
                continue
            self.groups.append(group)
            counts = Counter("active" if task["state"] in ACTIVE else task["state"] for task in members)
            options.append(Option(GroupPrompt(group["label"], counts, len(members),
                                              matching if (cast(StatusBrowser, self.app).statuses or
                                                           cast(StatusBrowser, self.app).name_query) else None,
                                              columns),
                                  id="group:" + group["key"]))
        yield self.listing(options, columns, "Groups")
        yield self.footer()

    def on_mount(self):
        self.set_focus(self.query_one(OptionList))

    def on_option_list_option_selected(self, event):
        group = self.groups[event.option_index]
        self.app.push_screen(TasksView(group["members"], [*self.breadcrumb, group["label"]], group["label"],
                                       group_key=group["key"]))


class TasksView(BrowserView):
    def __init__(self, members=None, breadcrumb=None, group=None, *, expanded=False, group_key=None):
        super().__init__(breadcrumb or ["Tasks"])
        self.members, self.group, self.expanded = members, group, expanded
        self.group_key = group_key
        self.items = []

    def compose(self) -> ComposeResult:
        self.items = []
        yield self.heading()
        yield self.scope()
        yield from self.notices()
        tasks = [task for task in self.snapshot['tasks'] if cast(StatusBrowser, self.app).matches(task)
                 and (self.members is None or task['name'] in self.members)]
        # Instance keys are captured from naming metadata: neither a qualified
        # group label nor splitting a name on '__' reliably identifies the key.
        names = [(task.get('instance_key', task['name']) or '(no key)') if self.group is not None
                 else task['name'] for task in tasks]
        states = [state_text(task['state']).cell_len for task in tasks]
        if self.expanded:
            states.extend(job_state_text(job).cell_len for task in tasks for job in task['jobs'])
        columns = ProgressColumns(max((cell_len(name) for name in names), default=8),
                                  max((len(f"{task['completed']}/{task['total']}") for task in tasks), default=3),
                                  progress_label="Progress", state_width=max(states, default=5),
                                  name_label="Instance" if self.group is not None else "Name",
                                  detail_width=max((cell_len(task_summary(task['detail'])) for task in tasks), default=0))
        options = []
        for task, name in zip(tasks, names):
            options.append(Option(TaskPrompt(task, columns, name), id="task:" + task["name"]))
            self.items.append((task, None))
            if self.expanded:
                for job in task["jobs"]:
                    options.append(Option(JobPrompt(job, columns), id="job:" + job["name"]))
                    self.items.append((task, job))
        yield self.listing(options, columns, self.group if self.group is not None else "Selected Tasks")
        yield self.footer()

    def on_mount(self):
        self.set_focus(self.query_one(OptionList))

    def footer(self):
        logs = [('l', 'Logs')] if self.expanded else []
        return self.shortcut_bar(('Enter', 'Open'), ('↑↓', 'Select'), *logs, ('/', 'Search'),
                                 ('f', 'Filter'), ('r', 'Refresh'), ('g', 'Groups'))

    def selection(self):
        index = self.query_one(OptionList).highlighted
        return self.items[index] if index is not None and index < len(self.items) else (None, None)

    def observe_snapshot(self):
        if self.group is None:
            return True
        group = next((group for group in self.snapshot["groups"] if group["key"] == self.group_key), None)
        if group is None or not any(cast(StatusBrowser, self.app).matches(task) for task in self.snapshot["tasks"]
                                    if task['name'] in group['members']):
            return False
        self.members = group["members"]
        self.group = group["label"]
        self.breadcrumb = [*self.breadcrumb[:-1], self.group]
        return True

    def on_option_list_option_selected(self, event):
        task, job = self.items[event.option_index]
        breadcrumb = [*self.breadcrumb, task["name"]]
        self.app.push_screen(TaskView(task, breadcrumb) if job is None else JobView(job, [*breadcrumb, job["local"]]))


class TaskView(BrowserView):
    BINDINGS = [Binding('u', 'reload_history', show=False)]
    DEFAULT_CSS = """
    TaskView .jobs-panel { min-height: 3; max-height: 50%; }
    TaskView .jobs-panel:focus-within { border: round $accent; }
    """

    def __init__(self, task, breadcrumb):
        super().__init__(breadcrumb)
        self.observation = task

    def summary(self):
        task = self.observation
        return Text.assemble(
            ("State:", "bold"), " ", state_text(task["state"]),
            ("\nJobs completed:", "bold"), " ", progress_display(task['completed'], task['total']),
            ("\nNext action:", "bold"), f" {task['next_action']}",
            ("\nReason:", "bold"), f" {format_reason(task['reason'])}",
        )

    def compose(self) -> ComposeResult:
        from .browser_evidence import HistoryPanel
        task = self.observation
        yield self.heading()
        with VerticalScroll(id='task-notices', classes='notice-pane', can_focus=False) as notices:
            notices.display = bool(self.snapshot.get('notices'))
            yield Static(Text(self.snapshot.get('notices', ''), style='yellow'))
        with VerticalScroll(can_focus=False):
            with Vertical(classes="detail-panel") as panel:
                panel.border_title = "Task"
                yield Static(self.summary(), id='task-summary')
            columns = JobColumns(task['jobs'])
            jobs = self.listing([Option(JobPrompt(job, columns), id="job:" + job["name"]) for job in task['jobs']],
                                columns, "Jobs", classes="jobs-panel", list_id='task-jobs')
            jobs.styles.height = len(task['jobs']) + 4
            yield jobs
            self.history = HistoryPanel(task, self.snapshot.get('storage'))
            yield self.history
        yield self.footer()

    def on_mount(self):
        self.set_focus(self.query_one('#task-jobs', OptionList), scroll_visible=False)

    def on_resize(self, event):
        super().on_resize(event)
        self.call_after_refresh(self.scroll_focused_panel)

    def scroll_focused_panel(self):
        if isinstance(self.focused, (OptionList, DataTable)):
            self.focused.parent.scroll_visible(animate=False)

    def footer(self):
        return self.shortcut_bar(('Enter', 'Open'), ('↑↓', 'Select'), ('Tab', 'Switch panel'),
                                 ('u', 'Reload history'), ('←→', 'Scroll history'), ('l', 'Logs'),
                                 ('r', 'Refresh'), ('g', 'Groups'), ('/', 'Search'), ('f', 'Filter'))

    def selection(self):
        if self.focused is self.history.query_one(DataTable):
            return self.history.selection()
        index = self.query_one('#task-jobs', OptionList).highlighted
        jobs = self.observation['jobs']
        return self.observation, jobs[index] if index is not None and index < len(jobs) else None

    def on_option_list_option_selected(self, event):
        if event.option_list.id == 'task-jobs':
            job = self.observation["jobs"][event.option_index]
            self.app.push_screen(JobView(job, [*self.breadcrumb, job["local"]]))

    def action_reload_history(self):
        self.history.reload()

    async def refresh_contents(self, navigation=None):
        navigation = self.capture_navigation() if navigation is None else navigation
        self.query_one('.breadcrumb', Static).update(Breadcrumb(self.breadcrumb))
        self.query_one('#task-summary', Static).update(self.summary())
        notices = self.query_one('#task-notices')
        notices.display = bool(self.snapshot.get('notices'))
        notices.query_one(Static).update(Text(self.snapshot.get('notices', ''), style='yellow'))
        jobs = self.observation['jobs']
        columns = JobColumns(jobs)
        self.query_one('.jobs-panel .list-columns', Static).update(columns)
        self.query_one('.jobs-panel').styles.height = len(jobs) + 4
        listing = self.query_one('#task-jobs', OptionList)
        listing.clear_options()
        listing.add_options(Option(JobPrompt(job, columns), id='job:' + job['name']) for job in jobs)
        self.app.call_after_refresh(self.restore_navigation, navigation)

    def observe_snapshot(self):
        task = next((task for task in self.snapshot["tasks"] if task["name"] == self.observation["name"]), None)
        if task is None or not cast(StatusBrowser, self.app).matches(task):
            return False
        self.observation = task
        return True


class ValueField(Static):
    """Show a literal field on one line, shortening long values in the middle."""

    DEFAULT_CSS = """
    ValueField { height: 1; }
    """

    def __init__(self, label, value, *, label_width, **kwargs):
        super().__init__(**kwargs)
        self.label, self.value, self.label_width = label, str(value), label_width

    def render(self):
        width = max(1, self.content_size.width - self.label_width - 1)
        return Text.assemble((self.label.ljust(self.label_width), "bold"), " ",
                             middle_name(self.value, width))

    def on_resize(self):
        self.refresh()


class LogField(ValueField, can_focus=True):
    """Select a log stream independently of whether its pathname is truncated."""

    BINDINGS = [Binding("up", "select_stream('stdout')", show=False),
                Binding("down", "select_stream('stderr')", show=False)]

    DEFAULT_CSS = """
    LogField:focus {
        background: $block-cursor-background;
        color: $block-cursor-foreground;
        text-style: bold;
    }
    """

    def __init__(self, stream, path):
        super().__init__(stream, path, label_width=len('stdout') + 1, id='log-' + stream)
        self.stream = stream

    def action_select_stream(self, stream):
        self.screen.query_one('#log-' + stream, LogField).focus()

    def on_key(self, event):
        if event.key == "enter":
            cast(JobDetailsView, self.screen).open_log(self.stream)
            event.stop()

    def on_click(self, event):
        self.focus()
        cast(JobDetailsView, self.screen).open_log(self.stream)
        event.stop()


class LogsPanel(Vertical):
    """A compact Kind/Path table with one selectable row per log stream."""

    DEFAULT_CSS = """
    LogsPanel LogField { padding: 0 2; }
    LogsPanel:focus-within .list-columns { background-tint: $foreground 5%; }
    """

    def __init__(self, logs):
        super().__init__(classes='detail-panel logs-panel')
        self.logs = logs
        self.border_title = 'Logs'

    def compose(self):
        with Vertical(classes='list-heading'):
            yield Static(Text(f"{'Kind':<7} Path", style='bold'), classes='list-columns')
        for stream in ('stdout', 'stderr'):
            yield LogField(stream, self.logs.get(stream, 'unavailable'))


class JobDetailsView(BrowserView):
    """Shared Job and Logs panels for current and historical submissions."""

    def compose(self) -> ComposeResult:
        yield self.heading()
        yield from self.notices(can_focus=False)
        yield from self.details()
        yield self.footer()

    def details(self):
        job = self.job
        recorded = 'recorded_state' in job
        label = 'Recorded state:' if recorded else 'Observed state:'
        if recorded:
            symbol, style = _VISUALS.get(job['observed_state'], ('?', 'yellow'))
            state = Text(f"{symbol} {job['recorded_state']}", style=style)
        else:
            state = state_text(job['observed_state'])
        with VerticalScroll(can_focus=False):
            with Vertical(classes="detail-panel") as panel:
                panel.border_title = "Job"
                yield Static(Text.assemble((label, "bold"), " ", state))
                if job["requires_repetition"]:
                    yield Static("Requires repetition for Task progress.")
                for label, value in (("Public job:", job['name']),
                                     ("Submission:", job['submission'] or 'not submitted'),
                                     ("Backend job:", job['backend_id'] if job['backend_id'] is not None else 'unavailable')):
                    yield ValueField(label, value, label_width=len("Observed state:"))
                task, _ = self.selection()
                yield ValueField('Attempt:', (task or {}).get('attempt') or 'not recorded',
                                 label_width=len('Observed state:'))
            yield LogsPanel(job['logs'])

    def footer(self):
        return self.shortcut_bar(('Enter', 'Open'), ('↑↓', 'Select stream'), ('Tab', 'Switch stream'),
                                 ('r', 'Refresh'), ('g', 'Groups'))

    def on_mount(self):
        self.set_focus(self.query_one(LogField))

    def open_log(self, stream):
        task, job = self.selection()
        if task is not None:
            from .browser_evidence import LogView
            self.app.push_screen(LogView(task, job, stream, self.breadcrumb))


class JobView(JobDetailsView):
    def __init__(self, job, breadcrumb):
        super().__init__(breadcrumb)
        self.job = job

    def selection(self):
        name = self.job['name'].removesuffix('__' + self.job['local'])
        task = next((task for task in self.snapshot['tasks'] if task['name'] == name), None)
        return task, self.job

    def observe_snapshot(self):
        owner = self.job['name'].removesuffix('__' + self.job['local'])
        job = next((job for task in self.snapshot["tasks"]
                    if task['name'] == owner and cast(StatusBrowser, self.app).matches(task)
                    for job in task["jobs"]
                    if job["name"] == self.job["name"]), None)
        if job is None:
            return False
        self.job = job
        return True


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
                         "In Task details, Tab switches Jobs/History; Enter opens a job or historical logs.\n"
                         "l: choose stdout/stderr for the selected job or historical submission\n"
                         "In Job details, use Up/Down and Enter to open a log; l is disabled.\n"
                         "u: reload the current log/history; status age stays unchanged\n"
                         "F: toggle live/manual log mode (starts manual; live checks about once per second)\n"
                         "Scrolling upward or Home switches to manual; F enables live mode. End does not enable it.\n"
                         "Leaving the log, including opening help, switches to manual and stops retries.\n"
                         "In logs, Page Up/Down scroll then read adjacent bounded portions; Home/End jump to first/latest.\n"
                         "w: toggle log wrapping (starts off); Left/Right scroll unwrapped lines.\n"
                         "Tab switches between log information and content.\n"
                         "Up/Down in log information scroll complete notices and errors.\n"
                         "In History, Left/Right scroll horizontally; Home/End go to the horizontal edges.\n"
                         "In search, Enter applies and Esc cancels; shortcut letters enter text.\n"
                         "In state filters, Space toggles, Enter applies, c clears, and Esc cancels.\n"
                         "/: literal Task-name search (ignores case)\nf: filter primary Task states\nr: inspect again\n"
                         "Ctrl+C: interrupt\n\nTask/job details use captured observations. "
                         "Logs/history read only the requested saved evidence. No workflow operations are executed here.")
            yield Static("Original Task selectors: " + ", ".join(cast(StatusBrowser, self.app).selectors or ("all Tasks",)),
                         markup=False)
            yield Static("Task state filter: " + ", ".join(cast(StatusBrowser, self.app).statuses or ("all",)),
                         markup=False)
            yield Static("Task name search: " + (cast(StatusBrowser, self.app).name_query or "none"), markup=False)
            if cast(StatusBrowser, self.app).navigation_notice:
                yield Static(cast(StatusBrowser, self.app).navigation_notice, markup=False)
            with self.section("Inspection output and diagnostics"):
                yield Static(Text.from_ansi(self.snapshot.get("inspection_output", "") + "\n" +
                                           self.snapshot.get("inspection_diagnostics", "")))
        yield self.footer()

    def footer(self):
        return self.shortcut_bar(('↑↓', 'Scroll'), ('Tab', 'Controls'), ('Enter', 'Toggle details'),
                                 ('g', 'Groups'), back='Close', show_help=False)


class SearchView(Screen):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        yield Static("Search Task names", classes="breadcrumb")
        yield InspectionStatus()
        yield Input(cast(StatusBrowser, self.app).name_query, placeholder="Literal text; ignores case")
        yield Static('Empty search: all selected Tasks', markup=False)
        yield Static(Shortcuts(('Enter', 'Apply'), back='Cancel', show_help=False, show_quit=False), classes='shortcuts')

    def on_mount(self):
        self.query_one(Input).focus()

    def action_cancel(self):
        self.app.pop_screen()

    async def on_input_submitted(self, event):
        browser = cast(StatusBrowser, self.app)
        browser.name_query = event.value
        self.app.pop_screen()
        await browser.update_views()
        event.stop()


class StateFilterView(Screen):
    BINDINGS = [Binding("escape", "cancel", show=False),
                Binding("enter", "apply", show=False, priority=True), Binding("c", "clear", show=False)]

    def check_action(self, action, parameters):
        if action == "apply":
            return isinstance(self.focused, SelectionList)
        return True

    def compose(self) -> ComposeResult:
        yield Static("Filter Task states", classes="breadcrumb")
        yield InspectionStatus()
        yield SelectionList(*((state_text(state), state, state in cast(StatusBrowser, self.app).statuses)
                              for state in STATES), classes="listing")
        yield Static('No choices means all states', markup=False)
        yield Static(Shortcuts(('Enter', 'Apply'), ('Space', 'Toggle'), ('↑↓', 'Select'), ('c', 'Clear'),
                              back='Cancel', show_quit=False), classes='shortcuts')

    def on_mount(self):
        self.query_one(SelectionList).focus()

    def action_cancel(self):
        self.app.pop_screen()

    def action_clear(self):
        self.query_one(SelectionList).deselect_all()

    async def action_apply(self):
        browser = cast(StatusBrowser, self.app)
        browser.statuses = tuple(self.query_one(SelectionList).selected)
        self.app.pop_screen()
        await browser.update_views()


class ErrorView(BrowserView):
    def __init__(self, failure, output, *, previous=False):
        super().__init__(["Refresh failed" if previous else "No snapshot available"])
        self.failure, self.output = failure, output
        self.previous = previous

    def heading(self):
        return super().heading() if self.previous else Static(Breadcrumb(self.breadcrumb), classes="breadcrumb")

    def compose(self) -> ComposeResult:
        yield self.heading()
        with VerticalScroll():
            yield Static(failure_summary(self.failure), markup=False)
            with self.section("Error details"):
                yield Static(Text.from_ansi(self.failure))
            if self.output:
                with self.section("Captured workflow output"):
                    yield Static(Text.from_ansi(self.output))
        yield self.shortcut_bar(('r', 'Retry'), ('Enter', 'Toggle details'), ('Tab', 'Controls'),
                                show_help=cast(StatusBrowser, self.app).snapshot is not None)

    def on_mount(self):
        self.set_focus(self.query_one(CollapsibleTitle))


class ResizeView(Screen):
    def compose(self) -> ComposeResult:
        yield Static("Terminal too small. Resize to at least 40 × 12.", markup=False)
        yield Static(Shortcuts(show_help=False), classes='shortcuts')


class Inspection:
    """Own one acquisition and all subprocesses it creates, never workflow jobs."""

    def __init__(self, arguments):
        self.directory = tempfile.TemporaryDirectory(prefix="gwflow-status-")
        self.root = Path(self.directory.name)
        self.result = self.root / "snapshot.json"
        self.started = time.monotonic()
        self.spinner = Spinner("dots")
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
            ("Inspecting ", "Preparing ", "Checking ", "Waiting ", "Pulling image:"))), "Loading workflow...")

    def progress(self, width):
        try:
            state = json.loads(self.result.with_suffix('.activity.json').read_text())
        except FileNotFoundError:
            state = dict(phase=self.phase(), total=None)
        state = state or dict(phase="Finalizing inspection...", total=None)
        if state['total'] is None:
            self.spinner.update(text=state['phase'])
            return self.spinner.render(time.monotonic())
        label = state['phase'].rstrip('.') + ': '
        fraction = f"{state['completed']}/{state['total']}"
        available = width - cell_len(label) - len(fraction) - 3
        separator = '' if available >= 3 else '\n'
        bar_width = min(14, available if available >= 3 else width - len(fraction) - 3)
        return Text(label + separator) + progress_display(state['completed'], state['total'], max(3, bar_width))

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
            # The unreaped supervisor still owns this PID / process group.
            # Never signal its numeric group after wait() has released it.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
        self.directory.cleanup()


class StatusBrowser(App[int]):
    CSS = """
    Screen { layout: vertical; }
    .view-heading { height: auto; margin-bottom: 1; }
    #breadcrumb, .breadcrumb { height: auto; text-style: bold; color: cyan; }
    #activity { height: auto; }
    #rows, .listing { height: 1fr; border: none; }
    .listing-panel { height: 1fr; margin-top: 1; border: round $foreground 25%; }
    .list-heading { height: auto; margin: 0 1; border-bottom: solid $foreground 20%; }
    .list-columns {
        height: auto; padding: 0 4 0 1;
        background: $panel; color: $foreground; text-style: bold;
    }
    .listing-panel:focus-within .list-columns { background-tint: $foreground 5%; }
    .table-list { padding: 0 1 0 2; scrollbar-gutter: stable; scrollbar-size-vertical: 2; }
    .table-list > .option-list--option { padding: 0; }
    .table-list:focus > .option-list--option-highlighted { text-style: bold; }
    .detail-panel { height: auto; margin-bottom: 1; padding: 0 2; border: round $foreground 25%; }
    .logs-panel { padding: 0; }
    .jobs-panel { height: 1fr; min-height: 8; }
    .compact .view-heading { margin-bottom: 0; }
    .compact .listing-panel { margin-top: 0; border: none; }
    .compact .list-heading { display: none; }
    .compact .logs-panel .list-heading { display: block; }
    .compact .table-list { padding: 0 1; }
    .compact .detail-panel { margin-bottom: 0; padding: 0; border: none; }
    .compact .jobs-panel { min-height: 3; }
    .jobs { height: 8; min-height: 3; max-height: 50%; border: none; }
    .notice-pane { height: auto; min-height: 2; max-height: 25%; border-left: wide yellow; }
    .compact .notice-pane { height: 1; min-height: 1; max-height: 1; }
    .read-info { height: auto; min-height: 1; max-height: 25%; }
    .compact .read-info { height: 2; max-height: 2; }
    #shortcuts, .shortcuts { height: 1; dock: bottom; }
    """
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("ctrl+c", "interrupt", show=False, priority=True),
                Binding("g", "groups", "Groups"), Binding("r", "refresh", "Refresh"),
                Binding("l", "logs", "Logs"),
                Binding("slash", "search", "Search"), Binding("f", "filter", "Filter"),
                Binding("question_mark", "help", "Help")]

    def __init__(self, arguments, *, selectors=(), statuses=(), details=False):
        super().__init__()
        self.no_color = False
        self.arguments = arguments
        self.selectors, self.statuses, self.details = selectors, statuses, details
        self.name_query = ""
        self.inspection = None
        self.snapshot: dict | None = None
        self.failure = ""
        self.navigation_notice = ""
        self.last_result = 1
        self.captured_output = ""
        self.interrupted = False
        self.too_small = False

    def get_line_filters(self):
        return [item for item in super().get_line_filters() if not isinstance(item, (NoColor, Monochrome))]

    def matches(self, task):
        return ((not self.statuses or task["state"] in self.statuses)
                and self.name_query.casefold() in task["name"].casefold())

    def compose(self) -> ComposeResult:
        yield Static("Groups", id="breadcrumb")
        yield Static("Loading workflow...", id="activity")
        yield OptionList(id="rows")
        yield Static(Shortcuts(show_help=False), id="shortcuts")

    def on_mount(self):
        self.action_refresh()
        self.set_interval(0.1, self.inspect_progress)

    def action_refresh(self):
        if self.inspection is None:
            if self.snapshot is None:
                while len(self.screen_stack) > 1:
                    self.pop_screen()
                if self.too_small:
                    self.push_screen(ResizeView())
            self.inspection = Inspection(self.arguments)

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

    def update_inspection_status(self):
        for screen in self.screen_stack:
            for status in screen.query(InspectionStatus):
                status.update_status()

    async def inspect_progress(self):
        self.update_inspection_status()
        if self.inspection is None:
            return
        elapsed = int(time.monotonic() - self.inspection.started)
        self.screen_stack[0].query_one("#activity", Static).update(
            self.inspection.progress(self.size.width) + f"\n{elapsed}s elapsed")
        if not self.inspection.completed():
            return
        try:
            snapshot, failure, output = self.inspection.outcome()
        finally:
            self.inspection.close()
            self.inspection = None
        await self.accept_inspection(snapshot, failure, output)

    async def accept_inspection(self, snapshot, failure="", output=""):
        """Install one completed observation, retaining the previous one on failure."""
        self.failure, self.captured_output = failure, output
        self.last_result = 0 if snapshot is not None else 1
        if snapshot is None and self.snapshot is not None:
            for screen in self.screen_stack:
                if isinstance(screen, ErrorView):
                    screen.failure, screen.output = failure, output
                    await screen.refresh_contents()
            self.update_inspection_status()
            return
        previous = self.snapshot
        self.snapshot = snapshot
        if previous is not None and snapshot is not None:
            self.navigation_notice = ""
            await self.update_views()
            return
        while len(self.screen_stack) > 1:
            self.pop_screen()
        if self.snapshot is None:
            self.show_snapshot_view(ErrorView(self.failure, self.captured_output))
            return
        self.show_snapshot_view(TasksView(expanded=self.details) if self.selectors or self.details else GroupsView())

    async def update_views(self):
        """Reconcile navigation with the current captured selection and filters."""
        with self.batch_update():
            renamed_paths = []
            views = list(self.screen_stack)[1:]
            for index, screen in enumerate(views):
                if not isinstance(screen, BrowserView):
                    continue
                for old, new in renamed_paths:
                    if screen.breadcrumb[:len(old)] == old:
                        screen.breadcrumb = [*new, *screen.breadcrumb[len(old):]]
                old_path = list(screen.breadcrumb)
                navigation = screen.capture_navigation()
                screen.available = not isinstance(screen, ErrorView) and screen.observe_snapshot()
                if not screen.available:
                    if any(isinstance(child, BrowserView) and child.pinned for child in views[index + 1:]):
                        continue
                    while screen in self.screen_stack:
                        self.pop_screen()
                    if not isinstance(screen, ErrorView):
                        parent = cast(BrowserView, self.screen).breadcrumb[-1]
                        self.navigation_notice = (f"Item no longer available: {screen.breadcrumb[-1]}; "
                                                  f"returned to {parent}.")
                    break
                if screen.breadcrumb != old_path:
                    renamed_paths.append((old_path, list(screen.breadcrumb)))
                await screen.refresh_contents(navigation)
        if self.too_small and not isinstance(self.screen, ResizeView):
            self.push_screen(ResizeView())
        self.update_inspection_status()

    def action_search(self):
        if not self.too_small and self.snapshot is not None and not isinstance(self.screen, SearchView):
            self.open_menu(SearchView())

    def action_logs(self):
        if isinstance(self.screen, JobDetailsView):
            return
        if not self.too_small and isinstance(self.screen, BrowserView):
            task, job = self.screen.selection()
            if task is not None and job is not None:
                from .browser_evidence import HistoricalJobView, StreamChooser
                if 'recorded_state' in job:
                    self.push_screen(HistoricalJobView(task, job, self.screen.breadcrumb))
                else:
                    self.push_screen(StreamChooser(task, job, self.screen.breadcrumb))

    def action_filter(self):
        if not self.too_small and self.snapshot is not None and not isinstance(self.screen, StateFilterView):
            self.open_menu(StateFilterView())

    def open_menu(self, view):
        if isinstance(self.screen, (HelpView, SearchView, StateFilterView, ErrorView)):
            self.switch_screen(view)
        else:
            self.push_screen(view)

    async def action_back(self):
        if not self.too_small and len(self.screen_stack) > 2:
            await self.pop_screen()
            if isinstance(self.screen, BrowserView) and not self.screen.available:
                await self.update_views()

    def action_groups(self):
        if not self.too_small and self.snapshot is not None:
            while len(self.screen_stack) > 2:
                self.pop_screen()
            self.switch_screen(GroupsView())

    def action_help(self):
        if not self.too_small and self.snapshot is not None and not isinstance(self.screen, HelpView):
            self.open_menu(HelpView())

    def action_quit(self):
        self.exit(self.last_result)

    def action_interrupt(self):
        self.interrupted = True
        self.action_quit()

    def close_inspection(self):
        if self.inspection is not None:
            self.inspection.close()
            self.inspection = None

    def _handle_exception(self, error):
        self.last_result = 1
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
