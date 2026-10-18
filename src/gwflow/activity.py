"""Elapsed activity for terminal frontends; one phase message for plain output."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
import sys

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .terminal import supports_decoration


_current = ContextVar("gwflow_activity", default=None)


@contextmanager
def pause_activity():
    current = _current.get()
    if current is not None:
        current[0].stop()
    try:
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
