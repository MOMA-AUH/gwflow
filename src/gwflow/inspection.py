"""Shared human-readable details for managed lifecycle inspection."""

from .workflow import lifecycle_jobs


def action_line(task):
    return f"Task {task.name}: {task.action}; {task.reason}"


def blockage_line(plan):
    return "No jobs will be submitted; blocked Tasks: " + ", ".join(task.name for task in plan.blocked)


def condition(task):
    if task.action == "reuse":
        return "reusable work-present" if task.work_present else "reusable work-cleaned"
    if task.action == "repair":
        return "repair-needed"
    if task.action == "transfer":
        return "results transfer"
    if task.action == "deferred":
        return "awaiting producer results"
    if task.action == "blocked":
        activity = task.consumers or any(item.state in ("active", "uncertain") for item in task.submissions.values())
        return "blocked activity" if activity else "blocked"
    if task.action == "active":
        finishing = task.submissions.get("gwflow_complete")
        if (finishing and finishing.state == "active"
                and all(task.submissions[local].state == "complete" for local in task.attempt["executions"])):
            return "results transfer (active)"
        return "incomplete computation (active)"
    return "incomplete computation"


def dependency_details(task):
    for name, expected in task.attempt["producers"].items():
        yield f"Expected producer {name}: {expected}"
    for name, jobs in task.consumers.items():
        yield f"Active consumer {name}: " + ", ".join(f"{item.submission} ({item.state})" for item in jobs)


def task_details(store, task):
    if task.attempt:
        yield f"Attempt: {task.attempt['attempt']}"
        yield f"Workspace: {store.workspace(task.attempt)}"
        yield f"Results: {store.result_dir(task.attempt)}"
        yield f"Staging: {store.transfer_dir(task.attempt)}"
        yield from dependency_details(task)
    else:
        yield "Attempt: not allocated; fresh execution will choose a new UUID"
    if task.action == "fresh" and task.attempt:
        yield "Next attempt: new UUID, allocated only by run"
    names = dict.fromkeys([*task.submissions, *lifecycle_jobs(task.structure["targets"])])
    for local in names:
        item = task.submissions.get(local)
        yield f"{task.name}__{local}: {item.submission if item and item.submission else 'not submitted'} ({item.state if item else 'pending'})"
        if item and item.job_id is not None:
            yield f"  Backend job: {item.job_id}"
        if item and item.submission:
            root = store.working_dir / ".gwf" / "logs"
            for suffix in ("stdout", "stderr"):
                yield f"  Log {suffix}: {root / (item.submission + '.' + suffix)}"
