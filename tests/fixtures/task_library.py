"""A task implementation imported by the pipeline fixture."""

from gwflow import Task


def text_task(stem):
    task = Task(inputs=["input.txt"], outputs=[f"{stem}.out"])
    task.target("prepare", inputs=["input.txt"], outputs=[f"{stem}.tmp"]) << (
        f"cat input.txt > {stem}.tmp"
    )
    task.target("finish", inputs=[f"{stem}.tmp"], outputs=[f"{stem}.out"]) << (
        f"tr '[:lower:]' '[:upper:]' < {stem}.tmp > {stem}.out"
    )
    return task
