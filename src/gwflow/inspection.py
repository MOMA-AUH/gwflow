"""Shared human-readable details for managed lifecycle inspection."""


def dependency_details(task):
    for name, expected in task.attempt["producers"].items():
        yield f"Expected producer {name}: {expected}"
    for name, jobs in task.consumers.items():
        yield f"Active consumer {name}: " + ", ".join(f"{item.submission} ({item.state})" for item in jobs)
