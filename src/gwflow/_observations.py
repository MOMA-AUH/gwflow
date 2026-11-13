"""Transient facts belonging to one planning pass, never mutation authority."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy


_current = ContextVar("gwflow_planning_observations", default=None)


def active():
    return _current.get() is not None


@contextmanager
def observation():
    """Bound a standalone inspection without discarding an enclosing pass."""
    if active():
        yield
    else:
        with planning_pass():
            yield


@contextmanager
def planning_pass():
    facts = {}
    with ExitStack() as resources:
        token = _current.set((facts, resources))
        try:
            yield
        finally:
            facts.clear()
            _current.reset(token)


def resource(kind, open_resource):
    """Retain a private read resource, closing it at every scope exit."""
    scope = _current.get()
    if scope is None:
        return None
    facts, resources = scope
    key = "resource", kind
    if key not in facts:
        facts[key] = resources.enter_context(open_resource())
    return facts[key]


def reuse(kind, key, observe, *, refresh=False, select=None):
    scope = _current.get()
    if scope is None:
        value = observe()
        return value if select is None else deepcopy(select(value))
    facts, _ = scope
    key = kind, key
    if refresh:
        facts.pop(key, None)
    if key not in facts:
        facts[key] = observe()
    # Callers own their observations, including nested admission records.
    # A private selector can extract a small value without copying a complete
    # history. It must not modify the stored observation; callers still own
    # the returned value, never the cached authority.
    value = facts[key] if select is None else select(facts[key])
    return deepcopy(value)
