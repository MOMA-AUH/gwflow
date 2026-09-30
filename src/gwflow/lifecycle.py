"""Owned storage and persistent evidence for the managed Task lifecycle."""

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from gwf.exceptions import WorkflowError
from gwf.utils import is_valid_name

from . import _files, inputs
from .commands import Command, shell
from .workflow import RetainedOutput, TargetOutput, relative_path, validate_destinations


def _uuid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _valid_identity(value):
    return (isinstance(value, dict) and set(value) == {"device", "inode"}
            and all(type(number) is int and number >= 0 for number in value.values()))


def _initialization_entries(path):
    with _files.directory(path) as directory:
        entries = set(os.listdir(directory))
    for filename in list(entries):
        if filename.startswith(".pending-") and _uuid(filename.removeprefix(".pending-")):
            # A killed atomic publish may leave its private regular file.
            # It is never read as evidence, selected, or deleted here.
            _files.metadata(path, [filename])
            entries.remove(filename)
    return entries


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _device(path):
    while not path.exists():
        path = path.parent
    return path.stat().st_dev


def _record(kind, **fields):
    return {"kind": "gwflow." + kind, "schema": 1, **fields}


def _matches(record, kind, **identity):
    return (isinstance(record, dict) and record.get("kind") == "gwflow." + kind
            and type(record.get("schema")) is int and record["schema"] == 1
            and all(record.get(key) == value for key, value in identity.items()))


def _valid_metadata(observations, paths):
    return (isinstance(observations, dict) and observations.keys() == set(paths)
            and all(isinstance(value, dict) and type(value.get("size")) is int
                    and value["size"] >= 0 and type(value.get("mtime_ns")) is int
                    for value in observations.values()))


def _fingerprint(structure, commands, tracking):
    definition = {"structure": structure, "commands": commands if tracking else None}
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


def target_dependencies(structure, local):
    return sorted({reference["target"] for reference in structure["targets"][local]["inputs"]
                   if isinstance(reference, dict) and "target" in reference})


def producer_names(structure):
    return sorted({reference["task"] for reference in structure["inputs"] if isinstance(reference, dict)})


def _boundary_reference(value, store, workflow):
    if isinstance(value, RetainedOutput):
        if (value.workflow is not workflow or value.task_name not in workflow._task_declarations
                or value.name not in workflow._task_declarations[value.task_name].retained):
            raise WorkflowError("Retained input must name a registered output in this Workflow")
        return {"task": value.task_name, "output": value.name}
    return inputs.declared_path(value, store.working_dir)


def ordered_targets(structure):
    return _dependency_order({local: set(target_dependencies(structure, local)) for local in structure["targets"]}, "Task target")


def ordered_tasks(declared):
    return _dependency_order({name: set(producer_names(structure)) for name, (structure, _) in declared.items()}, "Task")


def _dependency_order(remaining, kind):
    ordered = []
    while remaining:
        ready = sorted(local for local, dependencies in remaining.items() if not dependencies)
        if not ready:
            raise WorkflowError(f"{kind} dependency graph contains a cycle")
        ordered.extend(ready)
        for local in ready:
            del remaining[local]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return ordered


def _target_reference(task, reference):
    if (not isinstance(reference, TargetOutput) or reference.target not in task.targets.values()
            or reference.filename not in reference.target.outputs):
        raise WorkflowError("Target reference is not a declared input or output of its owning Task")
    return {"target": reference.target.name, "file": reference.filename}


def declarations(task, store, workflow):
    """Canonical logical structure and commands; generated paths never enter."""
    boundary = []
    for value in task.inputs:
        reference = _boundary_reference(value, store, workflow)
        if reference not in boundary:
            boundary.append(reference)
    boundary.sort(key=lambda item: json.dumps(item, sort_keys=True))
    if not task.targets:
        raise WorkflowError("Managed Tasks require at least one target")
    targets = {}
    commands = {}
    for name, target in sorted(task.targets.items()):
        if target.name != name:
            raise WorkflowError("A target's local name cannot change after declaration")
        if not target.outputs:
            raise WorkflowError(f"Task has outputless inner target {name!r}")
        outputs = [relative_path(path) for path in target.outputs]
        validate_destinations(outputs)
        incoming = []
        for value in target.inputs:
            reference = (_target_reference(task, value) if isinstance(value, TargetOutput)
                         else _boundary_reference(value, store, workflow))
            if not isinstance(value, TargetOutput) and reference not in boundary:
                raise WorkflowError(f"Target {name!r} uses an undeclared Task boundary input")
            if reference not in incoming:
                incoming.append(reference)
        incoming.sort(key=lambda item: json.dumps(item, sort_keys=True))
        targets[name] = {"inputs": incoming, "outputs": sorted(outputs)}
        if isinstance(target.spec, str):
            commands[name] = {"literal": target.spec}
        elif isinstance(target.spec, Command):
            bindings = {}
            for slot, reference in target.spec.bindings.items():
                if isinstance(reference, (str, os.PathLike, RetainedOutput)):
                    alias = _boundary_reference(reference, store, workflow)
                    if alias not in incoming:
                        raise WorkflowError(f"Command binding {slot!r} is not a declared target input")
                    bindings[slot] = {"external": alias} if isinstance(alias, str) else alias
                    continue
                resolved = _target_reference(task, reference)
                if reference.target is not target and resolved not in incoming:
                    raise WorkflowError(f"Command binding {slot!r} is not a declared input or output of {name!r}")
                bindings[slot] = resolved
            commands[name] = {"template": target.spec.template, "bindings": bindings}
        else:
            raise WorkflowError("A target command must be literal text or shell(template, **bindings)")
    retained = {}
    for name, (source, path) in sorted(task.retained.items()):
        if source.target not in task.targets.values() or source.filename not in source.target.outputs:
            raise WorkflowError(f"Retained output {name!r} has an undeclared source")
        retained[name] = {"target": source.target.name, "source": source.filename, "path": relative_path(path)}
    validate_destinations([item["path"] for item in retained.values()])
    structure = {"inputs": boundary, "targets": targets, "retained": retained}
    ordered_targets(structure)
    return structure, commands


def valid_command(command, target, local):
    try:
        if "literal" in command:
            return set(command) == {"literal"} and isinstance(command["literal"], str)
        if set(command) != {"template", "bindings"}:
            return False
        shell(command["template"], **command["bindings"])
        for binding in command["bindings"].values():
            if set(binding) == {"external"} and binding["external"] in target["inputs"]:
                continue
            if set(binding) == {"task", "output"} and binding in target["inputs"]:
                continue
            if (set(binding) != {"target", "file"} or not
                    ((binding["target"] == local and binding["file"] in target["outputs"]) or binding in target["inputs"])):
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError, WorkflowError):
        return False


def _valid_attempt(attempt):
    """Reject incomplete or incompatible records before using any stored paths."""
    try:
        structure, commands = attempt["structure"], attempt["commands"]
        targets, retained = structure["targets"], structure["retained"]
        if (not isinstance(structure["inputs"], list)
                or any(not (isinstance(path, str) and Path(path).is_absolute()
                            or isinstance(path, dict) and set(path) == {"task", "output"}
                            and isinstance(path["task"], str) and is_valid_name(path["task"])
                            and isinstance(path["output"], str) and path["output"])
                       for path in structure["inputs"])
                or not isinstance(attempt["producers"], dict) or set(attempt["producers"]) != set(producer_names(structure))
                or attempt["task"] in attempt["producers"] or any(not _uuid(value) for value in attempt["producers"].values())
                or not targets
                or set(targets) != set(attempt["executions"]) or set(targets) != set(commands)
                or set(attempt["jobs"]) != set(targets) | {"gwflow_prepare", "gwflow_complete"}
                or not _uuid(attempt["preparation"]) or not _uuid(attempt["operation"]) or type(attempt["command_tracking"]) is not bool
                or type(attempt["managed_tmpdir"]) is not bool):
            return False
        for local, target in targets.items():
            if (not isinstance(local, str) or not is_valid_name(local) or local.startswith("gwflow_")
                    or not _uuid(attempt["executions"][local]) or not isinstance(target["inputs"], list)
                    or any(not (reference in structure["inputs"]
                               or isinstance(reference, dict) and set(reference) == {"target", "file"}
                               and reference["target"] in targets and reference["file"] in targets[reference["target"]]["outputs"])
                           for reference in target["inputs"])
                    or not isinstance(target["outputs"], list) or not target["outputs"]
                    or any(relative_path(path) != path for path in target["outputs"])
                    or attempt["jobs"][local] != f"{attempt['task']}__{local}__{attempt['executions'][local]}"):
                return False
            validate_destinations(target["outputs"])
            if not valid_command(commands[local], target, local):
                return False
        for name, item in retained.items():
            if (not isinstance(name, str) or not name or item["target"] not in targets
                    or item["source"] not in targets[item["target"]]["outputs"]
                    or relative_path(item["path"]) != item["path"]):
                return False
        ordered_targets(structure)
        validate_destinations([item["path"] for item in retained.values()])
        if attempt["jobs"]["gwflow_prepare"] != f"{attempt['task']}__gwflow_prepare__{attempt['preparation']}":
            return False
        if attempt["jobs"]["gwflow_complete"] != f"{attempt['task']}__gwflow_complete__{attempt['operation']}":
            return False
        expected = _fingerprint(structure, commands, attempt["command_tracking"])
        return attempt["fingerprint"] == expected and relative_path(attempt["result_dir"]) == attempt["result_dir"]
    except (KeyError, TypeError, ValueError, AttributeError, WorkflowError):
        return False


@dataclass
class TaskObservation:
    name: str
    action: str
    reason: str
    structure: dict
    commands: dict
    attempt: dict | None = None
    work_present: bool = False
    submissions: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    retry: list = field(default_factory=list)
    consumers: dict = field(default_factory=dict)
    removal: dict | None = None


class Store:
    """Own locations, initialize attempts, and validate their durable evidence."""

    def __init__(self, working_dir, *, work_root="work", results_root="results", staging_root=None):
        self.working_dir = Path(working_dir).resolve()
        def resolve(value):
            path = Path(value)
            return (path if path.is_absolute() else self.working_dir / path).resolve()
        book = resolve(".gwf")
        self.locations = {"book": book, "work": resolve(work_root), "results": resolve(results_root),
                          "staging": resolve(staging_root) if staging_root is not None else book / "gwflow" / "staging"}
        self._validate_locations()
        self.root = book / "gwflow"
        self.owner_path = self.root / "owner.json"
        self.owner = _files.read_json(self.owner_path)
        if self.owner is None:
            if _files.exists(self.root):
                with _files.directory(self.root) as fd:
                    if os.listdir(fd):
                        raise WorkflowError("Missing or malformed managed ownership evidence; legacy records are not adopted")
        elif (not _matches(self.owner, "owner", task=None) or not _uuid(self.owner.get("owner"))
              or self.owner.get("working_dir") != str(self.working_dir)
              or self.owner.get("locations") != {key: str(path) for key, path in self.locations.items()}):
            raise WorkflowError("Managed ownership or recorded storage locations do not match; relocation/adoption is unsupported")
        if self.owner is not None:
            identities = self.owner.get("root_identity")
            if (not isinstance(identities, dict) or identities.keys() != self.locations.keys()
                    or any(not _valid_identity(value) for value in identities.values())):
                raise WorkflowError("Malformed managed root ownership evidence")
            pending = self.owner.get("work_recreation")
            if pending is not None:
                work = self.locations["work"]
                if (not isinstance(pending, dict) or not _uuid(pending.get("operation"))
                        or not _valid_identity(pending.get("identity"))
                        or pending.get("source") != str(work.with_name(f".{work.name}-gwflow-{pending['operation']}"))):
                    raise WorkflowError("Malformed work-root recreation evidence")
        self.validate_roots()

    @classmethod
    def for_workflow(cls, workflow):
        return cls(workflow.working_dir, work_root=workflow.work_root, results_root=workflow.results_root,
                   staging_root=workflow.results_staging_root)

    @classmethod
    def for_job(cls, owner_path):
        owner = _files.read_json(Path(owner_path))
        if not _matches(owner, "owner") or not isinstance(owner.get("locations"), dict):
            raise WorkflowError("Missing managed owner record")
        store = cls(owner["working_dir"], work_root=owner["locations"]["work"],
                    results_root=owner["locations"]["results"], staging_root=owner["locations"]["staging"])
        if store.owner_path != Path(owner_path):
            raise WorkflowError("Owner record location does not match")
        return store

    def _validate_locations(self):
        book, work, results, staging = (self.locations[key] for key in ("book", "work", "results", "staging"))
        if any(_overlap(a, b) for a, b in ((book, work), (book, results), (work, results))):
            raise WorkflowError("Work, results and bookkeeping roots must be disjoint and non-containing")
        if _overlap(staging, work) or _overlap(staging, results) or staging == book or staging in book.parents:
            raise WorkflowError("Results staging cannot overlap work/results or contain bookkeeping")
        if _device(staging) != _device(results):
            raise WorkflowError("Results staging must share the results filesystem; configure results_staging_root")

    def validate_roots(self):
        for key, path in self.locations.items():
            if not _files.exists(path):
                continue
            found = _files.identity(path)
            if self.owner is not None:
                expected = self.owner.get("root_identity", {}).get(key)
                pending = self.owner.get("work_recreation") if key == "work" else None
                if expected != found and (pending is None or pending["identity"] != found):
                    raise WorkflowError(f"Managed root identity changed: {path}")

    def work_root_needs_recreation(self):
        return self.owner is not None and (self.owner.get("work_recreation") is not None
                                           or not _files.exists(self.locations["work"]))

    def recreate_work_root(self):
        """Install a durably identified empty root before any result invalidation."""
        self.validate_roots()
        if not self.work_root_needs_recreation():
            return
        work = self.locations["work"]
        pending = self.owner.get("work_recreation")
        if pending is None:
            operation = uuid4().hex
            source = work.with_name(f".{work.name}-gwflow-{operation}")
            with _files.directory(source.parent, create=True) as parent:
                os.mkdir(source.name, dir_fd=parent)
                _files.sync_directory(parent)
            pending = {"operation": operation, "source": str(source), "identity": _files.identity(source)}
            self.owner = {**self.owner, "work_recreation": pending}
            _files.publish(self.owner_path, self.owner)
        source = Path(pending["source"])
        if _files.exists(work):
            if _files.identity(work) != pending["identity"] or _files.exists(source):
                raise WorkflowError("Work-root recreation destination changed")
        else:
            if _files.identity(source) != pending["identity"]:
                raise WorkflowError("Work-root recreation staging ownership changed")
            _files.commit_directory(source, work)
        self.owner = {**self.owner, "root_identity": {**self.owner["root_identity"], "work": pending["identity"]}}
        del self.owner["work_recreation"]
        _files.publish(self.owner_path, self.owner)

    def _task_dir(self, name):
        return self.root / "owners" / self.owner["owner"] / "tasks" / name

    def attempt_dir(self, attempt):
        return self._task_dir(attempt["task"]) / "attempts" / attempt["attempt"]

    def workspace(self, attempt):
        return self.locations["work"] / attempt["task"] / attempt["attempt"]

    def execution_dir(self, attempt, local):
        return self.workspace(attempt) / local / attempt["executions"][local]

    def result_dir(self, attempt):
        return self.locations["results"] / attempt["result_dir"]

    def transfer_dir(self, attempt):
        return self.locations["staging"] / attempt["attempt"] / attempt["operation"]

    def identity(self, attempt, **extra):
        return {"owner": self.owner["owner"], "task": attempt["task"], "attempt": attempt["attempt"], **extra}

    def job_identity(self, attempt, local):
        generation = ({"operation": attempt["operation"]} if local == "gwflow_complete"
                      else {"preparation": attempt["preparation"]} if local == "gwflow_prepare"
                      else {"execution": attempt["executions"][local]})
        return {"job": attempt["jobs"][local], **generation}

    def read(self, attempt, filename, kind, **extra):
        record = _files.read_json(self.attempt_dir(attempt) / filename)
        return record if _matches(record, kind, **self.identity(attempt, **extra)) else None

    def publish(self, attempt, filename, kind, **fields):
        self.validate_roots()
        if hasattr(self, "runtime_admission"):
            fields.setdefault("admission", self.runtime_admission)
        _files.publish(self.attempt_dir(attempt) / filename, _record(kind, **self.identity(attempt), **fields))

    def current(self, name, result_dir):
        destination = self.locations["results"] / result_dir
        if self.owner is None:
            if _files.exists(destination) or _files.exists(self.locations["work"] / name):
                raise WorkflowError(f"Unowned existing Task destination: {destination}")
            return None
        task_dir = self._task_dir(name)
        current = _files.read_json(task_dir / "current.json")
        if current is None:
            if (_files.exists(destination) or _files.exists(self.locations["work"] / name)
                    or _files.exists(task_dir) and not self.unselected_initialization(name)):
                raise WorkflowError(f"Missing ownership/attempt evidence for Task {name!r}; existing files are not adopted")
            return None
        if (not _matches(current, "current", owner=self.owner["owner"], task=name)
                or not _uuid(current.get("attempt"))):
            raise WorkflowError(f"Invalid current-attempt evidence for Task {name!r}")
        attempt = self.read(current, "attempt.json", "attempt")
        if attempt is None or not _valid_attempt(attempt):
            raise WorkflowError(f"Missing or malformed managed attempt for Task {name!r}")
        if attempt.get("result_dir") != result_dir:
            raise WorkflowError(f"Recorded Task results location changed for {name!r}")
        return attempt

    def unselected_initialization(self, name):
        """Recognize abandoned owned plans, never completed work or submissions."""
        task_dir = self._task_dir(name)
        if _files.exists(task_dir / "current.json") or _files.exists(self.locations["work"] / name):
            return False
        if _initialization_entries(task_dir) != {"attempts"}:
            return False
        with _files.directory(task_dir / "attempts") as directory:
            attempts = os.listdir(directory)
        if not attempts or any(not _uuid(value) for value in attempts):
            return False
        for attempt_id in attempts:
            attempt = self.read({"task": name, "attempt": attempt_id}, "attempt.json", "attempt")
            if attempt is None or not _valid_attempt(attempt) or _files.exists(self.result_dir(attempt)):
                return False
            for filename in _initialization_entries(self.attempt_dir(attempt)):
                if filename == "initialization.json":
                    record = self.initialization(attempt)
                    if record["previous"] is not None or record["result_identity"] is not None:
                        return False
                elif filename != "attempt.json":
                    return False
        return True

    def input_baseline(self, attempt):
        record = self.read(attempt, "inputs.json", "inputs", preparation=attempt["preparation"])
        if (record is None or record.get("producers") != attempt["producers"]
                or not inputs.valid(record.get("inputs"), self.input_paths(attempt))):
            raise WorkflowError("Missing or malformed input preparation baseline")
        return record

    def recorded_current(self, name):
        current = _files.read_json(self._task_dir(name) / "current.json")
        if (not _matches(current, "current", owner=self.owner["owner"], task=name)
                or not _uuid(current.get("attempt"))):
            raise WorkflowError(f"Missing selected producer attempt for Task {name!r}")
        record = self.read(current, "attempt.json", "attempt")
        if record is None or not _valid_attempt(record):
            raise WorkflowError(f"Malformed selected attempt for Task {name!r}")
        return self.current(name, record["result_dir"])

    def recorded_tasks(self):
        if self.owner is None:
            return []
        root = self.root / "owners" / self.owner["owner"] / "tasks"
        if not _files.exists(root):
            return []
        with _files.directory(root) as directory:
            names = sorted(os.listdir(directory))
        if any(not is_valid_name(name) for name in names):
            raise WorkflowError("Malformed recorded Task name")
        return [self.recorded_current(name) for name in names if not self.unselected_initialization(name)]

    def producer_attempt(self, attempt, name):
        producer = self.recorded_current(name)
        if producer["attempt"] != attempt["producers"][name]:
            raise WorkflowError(f"Expected producer {name} attempt {attempt['producers'][name]} changed; a fresh consumer attempt is required")
        return producer

    def retained_path(self, attempt, reference):
        producer = self.producer_attempt(attempt, reference["task"])
        if reference["output"] not in producer["structure"]["retained"]:
            raise WorkflowError("Producer does not declare the selected retained output")
        return self.result_dir(producer) / producer["structure"]["retained"][reference["output"]]["path"]

    def input_paths(self, attempt):
        return [reference if isinstance(reference, str) else str(self.retained_path(attempt, reference))
                for reference in attempt["structure"]["inputs"]]

    def require_producers(self, attempt):
        checking = getattr(self, "_checking_producers", set())
        if attempt["task"] in checking:
            raise WorkflowError("Recorded Task dependency graph contains a cycle")
        self._checking_producers = checking
        checking.add(attempt["task"])
        try:
            for name in attempt["producers"]:
                if not self.completed(self.producer_attempt(attempt, name)):
                    raise WorkflowError(f"Expected producer {name} attempt {attempt['producers'][name]} lacks checked Completion")
        finally:
            checking.remove(attempt["task"])

    def observe_inputs(self, attempt):
        self.require_producers(attempt)
        references = attempt["structure"]["inputs"]
        observed = inputs.observe([value for value in references if isinstance(value, str)], self.locations)
        for reference in references:
            if isinstance(reference, dict):
                path = self.retained_path(attempt, reference)
                observed[str(path)] = {"resolved": str(path), **_files.metadata(path.parent, [path.name])[path.name]}
        return observed

    def check_inputs(self, attempt):
        baseline = self.input_baseline(attempt)
        try:
            observed = self.observe_inputs(attempt)
        except WorkflowError as error:
            raise WorkflowError(f"External inputs unavailable after preparation; a fresh attempt is required: {error}") from error
        if baseline["inputs"] != observed:
            raise WorkflowError("External inputs changed after preparation; a fresh attempt is required")

    def prepare(self, attempt):
        path = self.attempt_dir(attempt) / "inputs.json"
        if _files.exists(path):
            self.check_inputs(attempt)
            return
        observed = self.observe_inputs(attempt)
        self.validate_roots()
        record = _record("inputs", **self.identity(attempt), preparation=attempt["preparation"], inputs=observed,
                         producers=attempt["producers"],
                         admission=getattr(self, "runtime_admission", None))
        try:
            _files.publish(path, record, replace=False)
        except FileExistsError:
            self.check_inputs(attempt)

    def write_execution(self, attempt, local, command, *, tracking):
        self.publish(attempt, f"executions/{attempt['executions'][local]}/definition.json", "execution",
                     execution=attempt["executions"][local], target=local, command=command, command_tracking=bool(tracking),
                     dependencies={name: attempt["executions"][name]
                                   for name in target_dependencies(attempt["structure"], local)})

    def execution(self, attempt, local):
        record = self.read(attempt, f"executions/{attempt['executions'][local]}/definition.json", "execution",
                           execution=attempt["executions"][local], target=local)
        dependencies = {name: attempt["executions"][name] for name in target_dependencies(attempt["structure"], local)}
        if (record is None or record.get("dependencies") != dependencies
                or type(record.get("command_tracking")) is not bool
                or not valid_command(record.get("command"), attempt["structure"]["targets"][local], local)
                or record["command_tracking"] and record["command"] != attempt["commands"][local]):
            raise WorkflowError(f"Missing or incompatible execution definition for {local!r}")
        return record

    def require_current_execution(self, attempt, local):
        current = self.current(attempt["task"], attempt["result_dir"])
        for name in [local, *target_dependencies(attempt["structure"], local)]:
            if current["executions"][name] != attempt["executions"][name]:
                raise WorkflowError(f"Execution generation changed for {name!r}")

    def replace_executions(self, observation, *, tracking):
        attempt = deepcopy(observation.attempt)
        if self.current(attempt["task"], attempt["result_dir"]) != attempt:
            raise WorkflowError("Task selection changed before retry")
        for local in observation.retry:
            execution = uuid4().hex
            attempt["executions"][local] = execution
            attempt["jobs"][local] = f"{attempt['task']}__{local}__{execution}"
        for local in observation.retry:
            self.write_execution(attempt, local, observation.commands[local], tracking=tracking)
        _files.publish(self.attempt_dir(attempt) / "attempt.json", attempt)
        return attempt

    def checked_target(self, attempt, local, *, check_files=True):
        record = self.read(attempt, f"executions/{attempt['executions'][local]}/success.json", "target-success",
                           execution=attempt["executions"][local], target=local)
        paths = attempt["structure"]["targets"][local]["outputs"]
        if (record is None or not _valid_metadata(record.get("outputs"), paths)
                or record.get("dependencies") != self.execution(attempt, local)["dependencies"]):
            raise WorkflowError(f"Target {local!r} lacks checked success evidence")
        if check_files and _files.metadata(self.execution_dir(attempt, local) / "committed", paths) != record["outputs"]:
            raise WorkflowError(f"Target {local!r} work metadata changed")
        return record

    def completed(self, attempt):
        self.check_inputs(attempt)
        operation = attempt["operation"]
        completion = self.read(attempt, "completion.json", "completion", operation=operation)
        installed = self.read(attempt, "installed.json", "installed", operation=operation)
        manifest = self.read(attempt, "manifest.json", "manifest", operation=operation)
        destinations = [item["path"] for item in attempt["structure"]["retained"].values()]
        if (completion is None or installed is None or manifest is None
                or completion.get("producers") != attempt["producers"]
                or completion.get("outputs") != installed.get("outputs")
                or installed.get("outputs") != manifest.get("outputs")
                or not _valid_metadata(completion.get("outputs"), destinations)):
            return False
        sources = {}
        for local in attempt["executions"]:
            target = self.checked_target(attempt, local, check_files=False)
            sources[local] = {"execution": target["execution"], "outputs": target["outputs"]}
        if (manifest.get("sources") != sources
                or manifest.get("retained") != attempt["structure"]["retained"]
                or manifest.get("destination") != str(self.result_dir(attempt))
                or manifest.get("staged_identity") != _files.identity(self.result_dir(attempt))):
            return False
        return _files.metadata(self.result_dir(attempt), destinations) == completion["outputs"]

    def result_removal(self, attempt):
        destination = self.result_dir(attempt)
        if not _files.exists(destination):
            return None
        if self.read(attempt, "ready.json", "ready") is None:
            expected = self.initialization(attempt)["result_identity"]
            if expected != _files.identity(destination):
                raise WorkflowError(f"Previous results ownership changed during initialization: {destination}")
            return expected
        manifest = self.read(attempt, "manifest.json", "manifest", operation=attempt["operation"])
        if (manifest is None or manifest.get("destination") != str(destination)
                or manifest.get("retained") != attempt["structure"]["retained"]
                or not _valid_metadata(manifest.get("outputs"), [item["path"] for item in attempt["structure"]["retained"].values()])
                or not isinstance(manifest.get("sources"), dict) or manifest["sources"].keys() != attempt["executions"].keys()
                or any(not isinstance(source, dict) or source.get("execution") != attempt["executions"][local]
                       or not _valid_metadata(source.get("outputs"), attempt["structure"]["targets"][local]["outputs"])
                       for local, source in manifest["sources"].items())
                or manifest.get("staged_identity") != _files.identity(destination)):
            raise WorkflowError(f"Cannot establish ownership of previous results: {destination}")
        return manifest["staged_identity"]

    def initialization(self, attempt):
        record = self.read(attempt, "initialization.json", "initialization")
        if (record is None or not {"previous", "result_identity"} <= record.keys()
                or record.get("destination") != str(self.result_dir(attempt))
                or record.get("previous") is not None and not _uuid(record["previous"])
                or record.get("result_identity") is not None and
                not _valid_identity(record["result_identity"])):
            raise WorkflowError("Missing or malformed Task initialization evidence")
        return record

    def initialization_previous(self, attempt):
        previous = self.initialization(attempt)["previous"]
        if previous is None:
            return None
        record = self.read({"task": attempt["task"], "attempt": previous}, "attempt.json", "attempt")
        if record is None or not _valid_attempt(record):
            raise WorkflowError("Missing previous-attempt evidence for initialization")
        return record

    def finish_initialization(self, attempt):
        self.validate_roots()
        record = self.initialization(attempt)
        if self.current(attempt["task"], attempt["result_dir"]) != attempt:
            raise WorkflowError("Task selection changed during initialization")
        if self.read(attempt, "ready.json", "ready") is not None:
            return attempt
        if _files.exists(self.attempt_dir(attempt) / "admissions"):
            raise WorkflowError("Unready Task has submission evidence; initialization cannot remove results")
        destination = self.result_dir(attempt)
        if _files.exists(destination):
            if record["result_identity"] is None:
                raise WorkflowError("Unexpected unowned results appeared during initialization")
            _files.remove_directory(destination, record["result_identity"])
        _files.mkdir(self.workspace(attempt))
        for local in ordered_targets(attempt["structure"]):
            self.write_execution(attempt, local, attempt["commands"][local], tracking=attempt["command_tracking"])
        self.publish(attempt, "ready.json", "ready")
        return attempt

    def initialize(self, observation, result_dir, *, tracking, managed_tmpdir, producers):
        self.validate_roots()
        if self.owner is None:
            owner = uuid4().hex
            for path in self.locations.values():
                _files.mkdir(path)
            self.owner = _record("owner", owner=owner, task=None, working_dir=str(self.working_dir),
                                 locations={key: str(path) for key, path in self.locations.items()},
                                 root_identity={key: _files.identity(path) for key, path in self.locations.items()})
            _files.publish(self.owner_path, self.owner)
        if self.current(observation.name, result_dir) != observation.attempt:
            raise WorkflowError("Task selection changed before initialization")
        attempt_id, operation, preparation = uuid4().hex, uuid4().hex, uuid4().hex
        executions = {local: uuid4().hex for local in ordered_targets(observation.structure)}
        jobs = {local: f"{observation.name}__{local}__{execution}" for local, execution in executions.items()}
        jobs["gwflow_prepare"] = f"{observation.name}__gwflow_prepare__{preparation}"
        jobs["gwflow_complete"] = f"{observation.name}__gwflow_complete__{operation}"
        fingerprint = _fingerprint(observation.structure, observation.commands, tracking)
        attempt = _record("attempt", owner=self.owner["owner"], task=observation.name, attempt=attempt_id,
                          operation=operation, preparation=preparation, executions=executions, jobs=jobs, result_dir=result_dir,
                          structure=observation.structure, commands=observation.commands, producers=producers,
                          command_tracking=bool(tracking), fingerprint=fingerprint, managed_tmpdir=managed_tmpdir)
        _files.publish(self.attempt_dir(attempt) / "attempt.json", attempt)
        self.publish(attempt, "initialization.json", "initialization", destination=str(self.result_dir(attempt)),
                     previous=observation.attempt["attempt"] if observation.attempt else None,
                     result_identity=observation.removal)
        _files.publish(self._task_dir(observation.name) / "current.json",
                       _record("current", **self.identity(attempt)))
        return self.finish_initialization(attempt)
