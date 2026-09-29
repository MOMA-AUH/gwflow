"""Reusable task for summarizing transaction amounts by product."""

from os import fspath
from shlex import quote
from sys import executable

from gwflow import Task


def summarize(source, intermediate, result):
    """Clean a transaction CSV, then retain its per-product totals."""
    source, intermediate, result = map(fspath, (source, intermediate, result))
    task = Task(inputs=[source], outputs=[result])
    command = f"{quote(executable)} -m summary_task"
    task.target("clean", inputs=[source], outputs=[intermediate]) << (
        f"{command} clean {quote(source)} {quote(intermediate)}"
    )
    task.target("aggregate", inputs=[intermediate], outputs=[result]) << (
        f"{command} aggregate {quote(intermediate)} {quote(result)}"
    )
    return task
