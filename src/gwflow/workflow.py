"""Logical Task declarations, independent of execution and storage paths."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from inspect import getfile
import os
from pathlib import Path, PurePosixPath
from sys import _getframe
from types import MappingProxyType

import click
from gwf import Target as GwfTarget, Workflow as GwfWorkflow
from gwf.exceptions import WorkflowError
from gwf.utils import is_valid_name


def _require_name(name, kind):
    if not isinstance(name, str) or not is_valid_name(name):
        raise WorkflowError(f"Invalid {kind} name: {name!r}")


def relative_path(value):
    try:
        value = os.fspath(value)
    except TypeError as exc:
        raise WorkflowError("Managed output paths must be relative filenames") from exc
    if (not isinstance(value, str) or not value or value.startswith("/")
            or ".." in value.split("/") or not PurePosixPath(value).parts
            or any(ord(char) < 32 for char in value) or any(char in value for char in "*?[]")):
        raise WorkflowError(f"Invalid managed relative path: {value!r}")
    return str(PurePosixPath(value))


def validate_destinations(paths):
    seen = set()
    for path in paths:
        if path in seen or any(path.startswith(other + "/") or other.startswith(path + "/")
                               for other in seen):
            raise WorkflowError(f"Managed output destination collision: {path}")
        seen.add(path)


@dataclass(frozen=True)
class TargetOutput:
    target: object
    filename: str


@dataclass(frozen=True)
class RetainedOutput:
    workflow: object
    task_name: str
    name: str

    def __deepcopy__(self, memo):
        # Registration snapshots declarations, but a reference retains the
        # identity of the Workflow that issued it.
        return self


@dataclass(frozen=True)
class TaskHandle:
    outputs: Mapping


@dataclass(eq=False)
class TaskTarget:
    name: str
    inputs: list
    outputs: list
    options: dict
    executor: object = None
    group: str | None = None
    spec: object = ""

    def output(self, filename):
        filename = relative_path(filename)
        if filename not in self.outputs:
            raise WorkflowError(f"Undeclared output {filename!r} on target {self.name!r}")
        return TargetOutput(self, filename)

    def __lshift__(self, command):
        self.spec = command
        return self


class Task:
    """A reusable definition with explicit inputs and named retained outputs."""

    def __init__(self, inputs, *, working_dir=None, defaults=None, executor=None):
        self.inputs = list(inputs)
        self.defaults = dict(defaults or {})
        self.executor = executor
        self.targets = {}
        self.retained = {}
        # Execution always uses managed staging, regardless of authoring CWD.
        self.working_dir = working_dir

    def target(self, name, inputs, outputs, *, executor=None, group=None, **options):
        _require_name(name, "local target")
        if name.startswith("gwflow_"):
            raise WorkflowError(f"Local target name {name!r} is reserved for bookkeeping")
        if name in self.targets:
            raise WorkflowError(f"Target {name!r} already exists in Task")
        if not isinstance(outputs, (list, tuple, set, frozenset)):
            raise WorkflowError("Target outputs must be an explicit finite list of regular files")
        outputs = [relative_path(path) for path in outputs]
        if not outputs:
            raise WorkflowError(f"Task has outputless inner target {name!r}")
        validate_destinations(outputs)
        target = TaskTarget(name, list(inputs), outputs, {**self.defaults, **options},
                            executor=executor or self.executor, group=group)
        self.targets[name] = target
        return target

    def retain(self, name, *, source, path):
        if not isinstance(name, str) or not name:
            raise WorkflowError("A retained output needs a nonempty public name")
        if name in self.retained:
            raise WorkflowError(f"Duplicate retained output name {name!r}")
        if (not isinstance(source, TargetOutput) or source.target not in self.targets.values()
                or source.filename not in source.target.outputs):
            raise WorkflowError("Retained source must be a declared output of this Task")
        path = relative_path(path)
        validate_destinations([*(item[1] for item in self.retained.values()), path])
        self.retained[name] = (source, path)


def lifecycle_jobs(targets):
    """Public job names in preparation/computation/finishing order."""
    return ["gwflow_prepare", *targets, "gwflow_complete"]


class _TaskTargets(dict):
    def values(self):
        cli = click.get_current_context(silent=True)
        if cli is not None and cli.info_name in ("clean", "touch"):
            raise WorkflowError("Generic clean/touch cannot mutate managed Tasks; use clean-work for work cleanup")
        return super().values()


class Workflow(GwfWorkflow):
    """Register Task factories and select their managed storage locations."""

    def __init__(self, working_dir=None, defaults=None, executor=None, *,
                 completion_defaults=None, preparation_defaults=None, managed_tmpdir=True,
                 work_root="work", results_root="results", results_staging_root=None):
        if working_dir is None:
            working_dir = str(Path(getfile(_getframe(1))).resolve().parent)
        kwargs = {"working_dir": str(Path(working_dir).absolute()), "defaults": defaults or {}}
        if executor is not None:
            kwargs["executor"] = executor
        super().__init__(**kwargs)
        self.completion_defaults = dict(completion_defaults or {})
        self.preparation_defaults = dict(preparation_defaults or {})
        if type(managed_tmpdir) is not bool:
            raise WorkflowError("managed_tmpdir must be true or false")
        self.managed_tmpdir = managed_tmpdir
        self.work_root = work_root
        self.results_root = results_root
        self.results_staging_root = results_staging_root
        self._task_declarations = {}
        self._result_dirs = {}
        self.targets = _TaskTargets()

    def _add_target(self, target):
        raise WorkflowError("Every computation target in gwflow.Workflow must belong to a Task")

    def task_from_template(self, name, task, *, result_dir=None):
        _require_name(name, "task")
        if not isinstance(task, Task):
            raise TypeError("task_from_template requires a Task definition")
        if name in self._task_declarations:
            raise WorkflowError(f"Task name {name!r} already exists in workflow")
        result_dir = relative_path(name if result_dir is None else result_dir)
        validate_destinations([*self._result_dirs.values(), result_dir])
        snapshot = deepcopy(task)
        names = {f"{name}__{local}" for local in lifecycle_jobs(snapshot.targets)}
        if names & self.targets.keys():
            raise WorkflowError("Task registration name collision")
        for local, target in snapshot.targets.items():
            target.options = {**self.defaults, **target.options}
            self.targets[f"{name}__{local}"] = GwfTarget(
                name=f"{name}__{local}", inputs=[], outputs=[], options=target.options,
                working_dir=self.working_dir, group=target.group,
            )
        for local in lifecycle_jobs([]):
            self.targets[f"{name}__{local}"] = GwfTarget(
                name=f"{name}__{local}", inputs=[], outputs=[], options={},
                working_dir=self.working_dir,
            )
        self._task_declarations[name] = snapshot
        self._result_dirs[name] = result_dir
        return TaskHandle(MappingProxyType({key: RetainedOutput(self, name, key)
                                            for key in snapshot.retained}))
