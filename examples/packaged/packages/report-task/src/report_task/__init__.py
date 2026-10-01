"""Reusable task for combining sales and returns summaries."""

from shlex import quote
from sys import executable

from gwflow import Task, shell


def net_report(sales, returns):
    """Join two retained summaries, then retain the net sales report."""
    task = Task(inputs=[sales, returns])
    command = f"{quote(executable)} -m report_task"
    join = task.target("join", inputs=[sales, returns], outputs=["joined.csv"])
    join << shell(
        f"{command} join {{sales}} {{returns}} {{destination}}",
        sales=sales, returns=returns, destination=join.output("joined.csv"),
    )
    finalize = task.target("finalize", inputs=[join.output("joined.csv")], outputs=["net.csv"])
    finalize << shell(
        f"{command} finalize {{source}} {{destination}}",
        source=join.output("joined.csv"), destination=finalize.output("net.csv"),
    )
    task.retain("report", source=finalize.output("net.csv"), path="net.csv")
    return task
