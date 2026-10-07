"""Transient facts belonging to one planning pass, never mutation authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy


_current = ContextVar("gwflow_planning_observations", default=None)


def active():
    return _current.get() is not None


@contextmanager
def planning_pass():
    facts = {}
    token = _current.set(facts)
    try:
        yield
    finally:
        facts.clear()
        _current.reset(token)


def reuse(kind, key, observe):
    facts = _current.get()
    if facts is None:
        return observe()
    key = kind, key
    if key not in facts:
        facts[key] = observe()
    # Callers own their observations, including nested admission records.
    return deepcopy(facts[key])
