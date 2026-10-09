"""Independent task implementation used by the README example."""

from gwflow import Task, shell, task_template


@task_template
def uppercase(source):
    task = Task(inputs=[source])
    copy = task.target("copy", inputs=[source], outputs=["copy.txt"])
    copy << shell("cat {source} > {out}", source=source, out=copy.output("copy.txt"))
    finish = task.target("finish", inputs=[copy.output("copy.txt")], outputs=["text.txt"])
    finish << shell(
        "tr '[:lower:]' '[:upper:]' < {source} > {out}",
        source=copy.output("copy.txt"), out=finish.output("text.txt"),
    )
    task.retain("text", source=finish.output("text.txt"), path="text.txt")
    return task
