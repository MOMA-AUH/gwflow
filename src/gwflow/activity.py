"""Elapsed phases and counted inspection; concise phase messages for plain output."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import logging
import os
import sys

import click
from rich.console import Console
from rich.live import Live
from rich.progress import Progress, ProgressColumn, SpinnerColumn, TextColumn, TimeElapsedColumn

from .terminal import supports_decoration


_current = ContextVar("gwflow_activity", default=None)
_report = ContextVar("gwflow_live_report", default=None)
_phase = ContextVar("gwflow_activity_phase", default=None)
_observer = ContextVar("gwflow_activity_observer", default=None)
_display = ContextVar("gwflow_activity_display", default=True)


@contextmanager
def observe_activity(callback, *, display=True):
    """Observe phases and counts; optionally let the observer own their display."""
    token = _observer.set(callback)
    display_token = _display.set(display)
    try:
        yield
    finally:
        _observer.reset(token)
        _display.reset(display_token)


class ActivityProgress(ProgressColumn):
    def __init__(self, console):
        super().__init__()
        self.console = console
        self.spinner = SpinnerColumn()

    def render(self, task):
        if task.total is None:
            return self.spinner.render(task)
        from .presentation import progress_display
        fraction = f"{int(task.completed)}/{int(task.total)}"
        width = max(3, min(14, self.console.width - len(task.description) - len(fraction) - 14))
        return progress_display(int(task.completed), int(task.total), width)


@contextmanager
def live_report(**options):
    """Register a report display so diagnostics remain above later refreshes."""
    display = Live(**options)

    @contextmanager
    def pause():
        nonlocal display
        display.stop()
        try:
            yield
        finally:
            # A stopped Live retains its cursor shape. Start a fresh display
            # below the diagnostic so refresh cannot erase that static text.
            display = Live(**options)
            display.start(refresh=True)

    token = _report.set(pause)
    try:
        display.start(refresh=True)
        yield
    finally:
        _report.reset(token)
        display.stop()


@contextmanager
def pause_activity():
    current = _current.get()
    report = _report.get()
    if current is not None:
        current[0].stop()
    try:
        with report() if report is not None else nullcontext():
            yield
    finally:
        if current is not None:
            current[0].start()


def message(value, *, err=False):
    """Keep complete receipts and diagnostics clear of a live activity line."""
    with pause_activity():
        click.echo(value, err=err, color=False)


class DiagnosticHandler(logging.StreamHandler):
    def emit(self, record):
        # Managed run reports own routine receipts; keep gwf's duplicate
        # per-target announcements available only in diagnostic debug output.
        if (record.name == "gwf.scheduling" and record.levelno == logging.INFO
                and str(record.msg).startswith("Submitted target ")
                and logging.getLogger().getEffectiveLevel() > logging.DEBUG):
            return
        with pause_activity():
            super().emit(record)


@contextmanager
def activity(phase, *, total=None):
    """Display a phase and yield a callback accepting its completed item count."""
    state = dict(phase=phase, completed=0, total=total)
    token = _phase.set(state)
    observer = _observer.get()

    def publish():
        if observer is not None:
            observer(_phase.get())

    try:
        publish()
        with _display_activity(phase, total) as update_display:
            def update(completed):
                state['completed'] = completed
                update_display(completed)
                publish()
            yield update
    finally:
        _phase.reset(token)
        publish()


@contextmanager
def _display_activity(phase, total):
    if not _display.get():
        yield lambda completed: None
        return
    context = click.get_current_context(silent=True)
    if context is None:
        yield lambda completed: None
        return
    formatted = (supports_decoration(sys.stdout) and supports_decoration(sys.stderr)
                 and not context.params.get("plain"))
    if not formatted:
        message(phase, err=True)
        yield lambda completed: None
        return
    parent = _current.get()
    if parent is None:
        console = Console(file=sys.stderr, no_color=bool(os.getenv("NO_COLOR")), markup=False, highlight=False)
        progress = Progress(TextColumn("{task.description}", markup=False), ActivityProgress(console), TimeElapsedColumn(),
                            console=console, transient=True, refresh_per_second=4,
                            redirect_stdout=False, redirect_stderr=False)
        progress.start()
    else:
        progress = parent[0]
        progress.update(parent[1], visible=False)
    task = progress.add_task(phase, total=total)
    token = _current.set((progress, task))
    try:
        yield lambda completed: progress.update(task, completed=completed, refresh=completed == total)
    finally:
        _current.reset(token)
        progress.remove_task(task)
        if parent is None:
            progress.stop()
        else:
            progress.update(parent[1], visible=True, refresh=True)
