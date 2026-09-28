"""Coordinated jobs for retrying one branch while other jobs stay active."""

from gwflow import Task


def gate(name):
    return (
        f"touch {name}.started; "
        f"while [ ! -f {name}.release ]; do sleep 0.05; done; "
    )


def retry_task(command="old"):
    task = Task(inputs=["input.txt"], outputs=["result.txt"])
    task.target("work", inputs=["input.txt"], outputs=["result.txt"]) << (
        "echo work >> trace.txt; test -f allow-work || exit 1; "
        + gate(command)
        + "if [ -f fail-after-release ]; then exit 1; fi; "
        + f"echo {command} > result.txt"
    )
    task.target("sibling", inputs=[], outputs=["sibling.tmp"]) << (
        gate("sibling") + "echo sibling >> trace.txt; touch sibling.tmp"
    )
    return task


def unrelated_task():
    task = Task(inputs=[], outputs=["unrelated.txt"])
    task.target("work", inputs=[], outputs=["unrelated.txt"]) << (
        gate("unrelated") + "echo unrelated >> trace.txt; touch unrelated.txt"
    )
    return task
