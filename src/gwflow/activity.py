"""Elapsed activity for terminal frontends; one phase message for plain output."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import logging
import os
import sys

import click
from rich.console import Console
from rich.live import Live
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .terminal import supports_decoration


_current = ContextVar("gwflow_activity", default=None)
_report = ContextVar("gwflow_live_report", default=None)


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
def activity(phase):
    context = click.get_current_context(silent=True)
    if context is None:
        yield
        return
    formatted = (supports_decoration(sys.stdout) and supports_decoration(sys.stderr)
                 and not context.params.get("plain"))
    if not formatted:
        message(phase, err=True)
        yield
        return
    parent = _current.get()
    if parent is None:
        console = Console(file=sys.stderr, no_color=bool(os.getenv("NO_COLOR")), markup=False, highlight=False)
        progress = Progress(SpinnerColumn(), TextColumn("{task.description}", markup=False), TimeElapsedColumn(),
                            console=console, transient=True, refresh_per_second=4,
                            redirect_stdout=False, redirect_stderr=False)
        progress.start()
    else:
        progress = parent[0]
        progress.update(parent[1], visible=False)
    task = progress.add_task(phase, total=None)
    token = _current.set((progress, task))
    try:
        yield
    finally:
        _current.reset(token)
        progress.remove_task(task)
        if parent is None:
            progress.stop()
        else:
            progress.update(parent[1], visible=True, refresh=True)
