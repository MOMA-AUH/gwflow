"""Resolve managed status names against the complete current workflow."""

from fnmatch import fnmatchcase

import click

from .workflow import lifecycle_jobs


def status_selection(plan, groups, selectors):
    """Return whole-Task membership and the automatic view before filtering."""
    tasks = {task.name: [task.name] for task in plan.tasks}
    if not selectors:
        return set(tasks), "overview"
    jobs = {f"{task.name}__{local}": [task.name]
            for task in plan.tasks for local in lifecycle_jobs(task.structure["targets"])}
    displayed_groups = {group.label: group.members for group in groups}
    selected = set()
    wildcard = False
    exact_group = False
    for selector in selectors:
        namespace, separator, name = selector.partition(":")
        if not separator or namespace not in ("task", "job"):
            namespace, name = None, selector
        candidates = [tasks] if namespace == "task" else [jobs] if namespace == "job" else [displayed_groups, tasks, jobs]
        if any(char in name for char in "*?["):
            wildcard = True
            for names in candidates:
                for candidate, members in names.items():
                    if fnmatchcase(candidate, name):
                        selected.update(members)
            continue
        if namespace is None:
            matching_groups = [group for group in groups if name in (group.prefix, group.qualified)]
            if len(matching_groups) > 1:
                choices = ", ".join(group.qualified for group in matching_groups)
                raise click.UsageError(f"Ambiguous group {selector!r}; choose one of: {choices}")
            if matching_groups:
                selected.update(matching_groups[0].members)
                exact_group = True
                continue
        for names in candidates:
            if name in names:
                selected.update(names[name])
                break
        else:
            raise click.UsageError(f"Unknown status selector: {selector!r}")
    view = "overview" if wildcard else "instances" if exact_group else "details"
    return selected, view
