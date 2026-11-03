"""Reusable task for summarizing transaction amounts by product."""

from shlex import quote
from sys import executable

from gwflow import task_template


@task_template
def summarize(source, *, image=None):
    """Clean a transaction CSV, then retain its per-product totals."""
    from gwflow import Task, shell

    task = Task(inputs=[source])
    interpreter = "python" if image is not None else quote(executable)
    command = f"{interpreter} -m summary_task"
    clean = task.target("clean", inputs=[source], outputs=["cleaned.csv"], image=image)
    clean << shell(
        f"{command} clean {{source}} {{destination}}", source=source, destination=clean.output("cleaned.csv")
    )
    aggregate = task.target("aggregate", inputs=[clean.output("cleaned.csv")], outputs=["summary.csv"], image=image)
    aggregate << shell(
        f"{command} aggregate {{source}} {{destination}}",
        source=clean.output("cleaned.csv"), destination=aggregate.output("summary.csv"),
    )
    task.retain("summary", source=aggregate.output("summary.csv"), path="summary.csv")
    return task
