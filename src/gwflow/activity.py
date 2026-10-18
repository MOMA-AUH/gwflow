"""Elapsed activity for terminal frontends; one phase message for plain output."""

from contextlib import contextmanager
import os
import sys

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn


@contextmanager
def activity(phase):
    context = click.get_current_context(silent=True)
    if context is None:
        yield
        return
    formatted = (sys.stdout.isatty() and sys.stderr.isatty() and not context.params.get("plain")
                 and os.environ.get("TERM", "").lower() not in ("dumb", "unknown"))
    try:
        for stream in (sys.stdout, sys.stderr):
            "╭╮╰╯│─━╸╺○◷↻▶↗✓◇…✗⊘".encode(stream.encoding or "utf-8")
    except UnicodeEncodeError:
        formatted = False
    if not formatted:
        click.echo(phase, err=True, color=False)
        yield
        return
    console = Console(file=sys.stderr, no_color=bool(os.getenv("NO_COLOR")), markup=False, highlight=False)
    with Progress(SpinnerColumn(), TextColumn("{task.description}", markup=False), TimeElapsedColumn(),
                  console=console, transient=True, refresh_per_second=4,
                  redirect_stdout=False, redirect_stderr=False) as progress:
        progress.add_task(phase, total=None)
        yield
