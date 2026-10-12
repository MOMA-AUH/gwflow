"""Resolve command selections against their available Task names."""

from fnmatch import fnmatchcase

import click


def task_selection(names, selectors):
    """Select whole Tasks, rejecting every unmatched exact name or glob."""
    names = set(names)
    if not selectors:
        return names
    selected = set()
    unmatched = []
    for selector in selectors:
        matches = {name for name in names if fnmatchcase(name, selector)}
        if not matches:
            unmatched.append(selector)
        selected.update(matches)
    if unmatched:
        raise click.UsageError("Unmatched Task selectors: " + ", ".join(repr(value) for value in unmatched))
    return selected
