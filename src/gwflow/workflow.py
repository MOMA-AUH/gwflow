"""Task authoring and boundary validation for ordinary gwf targets."""

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from inspect import getfile
from os import fspath, getcwd
from os.path import abspath, isabs, join
from pathlib import Path
from sys import _getframe

from gwf import Workflow as GwfWorkflow
from gwf.exceptions import WorkflowError
from gwf.utils import is_valid_name


def _require_name(name, kind):
    if not isinstance(name, str) or not is_valid_name(name):
        raise WorkflowError(f"Invalid {kind} name: {name!r}")


def _target_name(task_name, local_name):
    return f"{task_name}__{local_name}"


def _bookkeeping_name(task_name):
    return _target_name(task_name, "gwflow_complete")


def _paths(working_dir, declaration):
    if isinstance(declaration, str) or hasattr(declaration, "__fspath__"):
        path = fspath(declaration)
        return {path if isabs(path) else abspath(join(working_dir, path))}
    if isinstance(declaration, Mapping):
        declaration = declaration.values()
    return {path for item in declaration for path in _paths(working_dir, item)}


class _TaskTargets(dict):
    """Make task declarations available when gwf builds its target graph."""

    def __init__(self, workflow):
        super().__init__()
        self.workflow = workflow

    def values(self):
        self.workflow._validate_task_boundaries()
        from .reuse import materialize

        return materialize(self.workflow, list(super().values()))


class Task(GwfWorkflow):
    """An independent definition of ordinary file-producing gwf targets.

    ``inputs`` and ``outputs`` declare the external inputs and retained outputs.
    The declarations are validated before gwf builds the dependency graph.
    Unless ``working_dir`` is supplied, targets inherit the registering
    workflow's working directory.
    """

    def __init__(self, inputs, outputs, *, working_dir=None, defaults=None, executor=None):
        kwargs = {
            "working_dir": getcwd() if working_dir is None else working_dir,
            "defaults": defaults or {},
        }
        if executor is not None:
            kwargs["executor"] = executor
        super().__init__(**kwargs)
        self.inputs = deepcopy(inputs)
        self.outputs = deepcopy(outputs)
        self._explicit_working_dir = working_dir is not None


class Workflow(GwfWorkflow):
    """A gwf workflow that accepts named snapshots of Task definitions."""

    def __init__(self, working_dir=None, defaults=None, executor=None):
        if working_dir is None:
            working_dir = str(Path(getfile(_getframe(1))).resolve().parent)
        kwargs = {"working_dir": working_dir, "defaults": defaults or {}}
        if executor is not None:
            kwargs["executor"] = executor
        super().__init__(**kwargs)
        self._task_declarations = {}
        self._reserved_names = set()
        self.targets = _TaskTargets(self)

    def _validate_task_boundaries(self):
        """Check task boundaries before gwf submits any target."""
        produced_by = defaultdict(list)
        retained = {}
        for name, declaration in self._task_declarations.items():
            _, outputs, target_names, working_dir = declaration
            retained[name] = _paths(working_dir, outputs)
            for target_name in target_names:
                for path in self.targets[target_name].flattened_outputs():
                    produced_by[path].append(name)

        for name, declaration in self._task_declarations.items():
            inputs, outputs, target_names, working_dir = declaration
            external = _paths(working_dir, inputs)
            produced = {
                path
                for target_name in target_names
                for path in self.targets[target_name].flattened_outputs()
            }
            for path in _paths(working_dir, outputs) - produced:
                raise WorkflowError(
                    f"Task {name!r} declares retained output {path!r} "
                    "without a producing target"
                )
            for target_name in target_names:
                target = self.targets[target_name]
                if not target.flattened_outputs():
                    raise WorkflowError(
                        f"Task {name!r} has outputless inner target {target_name!r}"
                    )
                for path in target.flattened_inputs():
                    if path not in produced and path not in external:
                        raise WorkflowError(
                            f"Task {name!r} target {target_name!r} uses {path!r} "
                            "without declaring it as an external input"
                        )
                    producers = produced_by[path]
                    if len(producers) == 1 and producers[0] != name:
                        producer = producers[0]
                        if path not in retained[producer]:
                            raise WorkflowError(
                                f"Task {name!r} consumes {path!r} from task "
                                f"{producer!r}, but it is not a retained output"
                            )

    def _add_target(self, target):
        if target.name in self._reserved_names:
            raise WorkflowError(
                f"Target name {target.name!r} is reserved for task bookkeeping"
            )
        super()._add_target(target)

    def task_from_template(self, name, task):
        """Register a named, independent snapshot of ``task``.

        A factory can return a new Task for each instance. Its local target
        names may then be reused in another instance; file paths stay as
        supplied by the author.
        """

        _require_name(name, "task")
        if not isinstance(task, Task):
            raise TypeError("task_from_template requires a Task definition")
        if name in self._task_declarations:
            raise WorkflowError(f"Task name {name!r} already exists in workflow")

        reserved = _bookkeeping_name(name)
        new_targets = []
        for local_name, target in task.targets.items():
            _require_name(local_name, "local target")
            if local_name == "gwflow_complete":
                raise WorkflowError(
                    f"Local target name {local_name!r} is reserved for task bookkeeping"
                )
            snapshot = deepcopy(target)
            snapshot.name = _target_name(name, local_name)
            if not task._explicit_working_dir:
                snapshot.working_dir = self.working_dir
            snapshot.options = {**self.defaults, **snapshot.options}
            new_targets.append(snapshot)

        proposed = {target.name for target in new_targets}
        if len(proposed) != len(new_targets):
            raise WorkflowError(f"Task {name!r} produces duplicate qualified target names")
        collisions = (proposed | {reserved}) & (
            set(self.targets) | self._reserved_names
        )
        if reserved in proposed:
            collisions.add(reserved)
        if collisions:
            raise WorkflowError(
                "Task registration name collision: " + ", ".join(sorted(collisions))
            )

        declarations = (
            deepcopy(task.inputs),
            deepcopy(task.outputs),
            tuple(target.name for target in new_targets),
            task.working_dir if task._explicit_working_dir else self.working_dir,
        )
        for target in new_targets:
            self._add_target(target)
        self._task_declarations[name] = declarations
        self._reserved_names.add(reserved)
