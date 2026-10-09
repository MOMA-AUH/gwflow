"""Reusable task for combining sales and returns summaries."""

from shlex import quote
from sys import executable

from gwflow import task_template


@task_template
def net_report(sales, returns, *, image=None):
    """Join two retained summaries, then retain the net sales report."""
    from gwflow import Task, shell

    task = Task(inputs=[sales, returns])
    interpreter = "python" if image is not None else quote(executable)
    command = f"{interpreter} -m report_task"
    join = task.target(
        "join", inputs=[sales, returns], outputs=["joined.csv"], image=image,
        stage_as={"sales/summary.csv": sales, "returns/summary.csv": returns} if image is not None else {},
    )
    join << shell(
        f"{command} join {{sales}} {{returns}} {{destination}}",
        sales=sales, returns=returns, destination=join.output("joined.csv"),
    )
    finalize = task.target("finalize", inputs=[join.output("joined.csv")], outputs=["net.csv"], image=image)
    finalize << shell(
        f"{command} finalize {{source}} {{destination}}",
        source=join.output("joined.csv"), destination=finalize.output("net.csv"),
    )
    task.retain("report", source=finalize.output("net.csv"), path="net.csv")
    return task
