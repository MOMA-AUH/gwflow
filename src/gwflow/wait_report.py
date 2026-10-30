"""Compact waiting observations on stderr, with a live terminal view."""

from contextlib import nullcontext
import os
import sys

import click
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .presentation import _VISUALS, progress_display
from .terminal import supports_decoration


class WaitReport:
    def __init__(self):
        self.console = Console(file=sys.stderr, no_color=bool(os.getenv('NO_COLOR')),
                               markup=False, highlight=False)
        self.decorated = supports_decoration(sys.stderr)
        self.tasks = []
        self.previous = {}
        self.phase = 'Inspecting workflow...'

    def live(self):
        return (Live(get_renderable=self.render, console=self.console, refresh_per_second=4,
                     redirect_stdout=False, redirect_stderr=False)
                if self.decorated else nullcontext())

    def render(self):
        table = Table.grid(padding=(0, 2))
        for task in self.tasks:
            icon, style = _VISUALS[task['state']]
            table.add_row(Text(task['name']), Text(f"{icon} {task['state']}", style=style),
                          progress_display(task['completed'], task['total'], width=10),
                          Text('' if task['routine'] else task['detail']))
        return Group(Text(self.phase, style='dim'), table)

    def update(self, tasks):
        self.tasks = tasks
        complete = sum(task['complete'] for task in tasks)
        self.phase = f"Waiting · {complete}/{len(tasks)} Tasks complete"
        for task in tasks:
            # Routine scheduler churn belongs in the live progress, not the log.
            detail = '' if task['routine'] else task['reason']
            message = f"{task['name']}: {task['state']}" + (f"; {detail}" if detail else '')
            if self.previous.get(task['name']) != message:
                if not self.decorated or not task['routine']:
                    self.message(message)
                self.previous[task['name']] = message

    def message(self, message, *, nl=True):
        if self.decorated:
            self.console.print(Text(message.rstrip('\n')), end='\n' if nl or message.endswith('\n') else '')
        else:
            click.echo(message, err=True, nl=nl)
