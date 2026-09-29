"""Reusable task for combining sales and returns summaries."""

from os import fspath
from shlex import quote
from sys import executable

from gwflow import Task


def net_report(sales, returns, intermediate, result):
    """Join two retained summaries, then retain the net sales report."""
    sales, returns, intermediate, result = map(
        fspath, (sales, returns, intermediate, result)
    )
    task = Task(inputs=[sales, returns], outputs=[result])
    command = f"{quote(executable)} -m report_tasks"
    task.target("join", inputs=[sales, returns], outputs=[intermediate]) << (
        f"{command} join {quote(sales)} {quote(returns)} {quote(intermediate)}"
    )
    task.target("finalize", inputs=[intermediate], outputs=[result]) << (
        f"{command} finalize {quote(intermediate)} {quote(result)}"
    )
    return task
