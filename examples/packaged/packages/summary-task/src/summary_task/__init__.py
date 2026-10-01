"""Reusable task for summarizing transaction amounts by product."""

from shlex import quote
from sys import executable

from gwflow import Task, shell


def summarize(source):
    """Clean a transaction CSV, then retain its per-product totals."""
    task = Task(inputs=[source])
    command = f"{quote(executable)} -m summary_task"
    clean = task.target("clean", inputs=[source], outputs=["cleaned.csv"])
    clean << shell(
        f"{command} clean {{source}} {{destination}}", source=source, destination=clean.output("cleaned.csv")
    )
    aggregate = task.target("aggregate", inputs=[clean.output("cleaned.csv")], outputs=["summary.csv"])
    aggregate << shell(
        f"{command} aggregate {{source}} {{destination}}",
        source=clean.output("cleaned.csv"), destination=aggregate.output("summary.csv"),
    )
    task.retain("summary", source=aggregate.output("summary.csv"), path="summary.csv")
    return task
