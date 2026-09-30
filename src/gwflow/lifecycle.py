"""Owned storage and persistent evidence for the managed Task lifecycle."""

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
from .workflow import TargetOutput, relative_path, validate_destinations


def _uuid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


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


def declarations(task, store):
    """Canonical logical structure and commands; generated paths never enter."""
    boundary = sorted({inputs.declared_path(value, store.working_dir) for value in task.inputs})
    if len(task.targets) != 1:
        raise WorkflowError("Managed Tasks currently require exactly one target")
    targets = {}
    commands = {}
    for name, target in sorted(task.targets.items()):
        if target.name != name:
            raise WorkflowError("A target's local name cannot change after declaration")
        if not target.outputs:
            raise WorkflowError(f"Task has outputless inner target {name!r}")
        outputs = [relative_path(path) for path in target.outputs]
        validate_destinations(outputs)
        incoming = sorted({inputs.declared_path(value, store.working_dir) for value in target.inputs})
        if not set(incoming) <= set(boundary):
            raise WorkflowError(f"Target {name!r} uses an undeclared Task boundary input")
        targets[name] = {"inputs": incoming, "outputs": sorted(outputs)}
        if isinstance(target.spec, str):
            commands[name] = {"literal": target.spec}
        elif isinstance(target.spec, Command):
            bindings = {}
            for slot, reference in target.spec.bindings.items():
                if isinstance(reference, (str, os.PathLike)):
                    alias = inputs.declared_path(reference, store.working_dir)
                    if alias not in incoming:
                        raise WorkflowError(f"Command binding {slot!r} is not a declared target input")
                    bindings[slot] = {"external": alias}
                    continue
                if (not isinstance(reference, TargetOutput) or reference.target is not target
                        or reference.filename not in target.outputs):
                    raise WorkflowError(f"Command binding {slot!r} is not a declared input or output of {name!r}")
                bindings[slot] = {"target": name, "file": reference.filename}
            commands[name] = {"template": target.spec.template, "bindings": bindings}
        else:
            raise WorkflowError("A target command must be literal text or shell(template, **bindings)")
    retained = {}
    for name, (source, path) in sorted(task.retained.items()):
        if source.target not in task.targets.values() or source.filename not in source.target.outputs:
            raise WorkflowError(f"Retained output {name!r} has an undeclared source")
        retained[name] = {"target": source.target.name, "source": source.filename, "path": relative_path(path)}
    validate_destinations([item["path"] for item in retained.values()])
    return {"inputs": boundary, "targets": targets, "retained": retained}, commands


def _valid_attempt(attempt):
    """Reject incomplete or incompatible records before using any stored paths."""
    try:
        structure, commands = attempt["structure"], attempt["commands"]
        targets, retained = structure["targets"], structure["retained"]
        if (not isinstance(structure["inputs"], list)
                or any(not isinstance(path, str) or not Path(path).is_absolute() for path in structure["inputs"])
                or not targets
                or set(targets) != set(attempt["executions"]) or set(targets) != set(commands)
                or set(attempt["jobs"]) != set(targets) | {"gwflow_prepare", "gwflow_complete"}
                or not _uuid(attempt["preparation"]) or not _uuid(attempt["operation"]) or type(attempt["command_tracking"]) is not bool
                or type(attempt["managed_tmpdir"]) is not bool):
            return False
        for local, target in targets.items():
            if (not isinstance(local, str) or not is_valid_name(local) or local.startswith("gwflow_")
                    or not _uuid(attempt["executions"][local]) or not isinstance(target["inputs"], list)
                    or not set(target["inputs"]) <= set(structure["inputs"])
                    or not isinstance(target["outputs"], list) or not target["outputs"]
                    or any(relative_path(path) != path for path in target["outputs"])
                    or attempt["jobs"][local] != f"{attempt['task']}__{local}__{attempt['executions'][local]}"):
                return False
            validate_destinations(target["outputs"])
            command = commands[local]
            if "literal" in command:
                if set(command) != {"literal"} or not isinstance(command["literal"], str):
                    return False
            else:
                if set(command) != {"template", "bindings"}:
                    return False
                shell(command["template"], **command["bindings"])
                for binding in command["bindings"].values():
                    if set(binding) == {"external"} and binding["external"] in target["inputs"]:
                        continue
                    if set(binding) != {"target", "file"} or binding["target"] != local or binding["file"] not in target["outputs"]:
                        return False
        for name, item in retained.items():
            if (not isinstance(name, str) or not name or item["target"] not in targets
                    or item["source"] not in targets[item["target"]]["outputs"]
                    or relative_path(item["path"]) != item["path"]):
                return False
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
                    or any(not isinstance(value, dict) or set(value) != {"device", "inode"}
                           or any(type(number) is not int or number < 0 for number in value.values())
                           for value in identities.values())):
                raise WorkflowError("Malformed managed root ownership evidence")
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
        if _device(work) != _device(results):
            raise WorkflowError("Separate work/results filesystems are not yet supported")

    def validate_roots(self):
        for key, path in self.locations.items():
            if not _files.exists(path):
                continue
            found = _files.identity(path)
            if self.owner is not None:
                expected = self.owner.get("root_identity", {}).get(key)
                if expected != found:
                    raise WorkflowError(f"Managed root identity changed: {path}")

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
            if _files.exists(task_dir) or _files.exists(destination) or _files.exists(self.locations["work"] / name):
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

    def input_baseline(self, attempt):
        record = self.read(attempt, "inputs.json", "inputs", preparation=attempt["preparation"])
        if record is None or not inputs.valid(record.get("inputs"), attempt["structure"]["inputs"]):
            raise WorkflowError("Missing or malformed input preparation baseline")
        return record

    def check_inputs(self, attempt):
        baseline = self.input_baseline(attempt)
        try:
            observed = inputs.observe(attempt["structure"]["inputs"], self.locations)
        except WorkflowError as error:
            raise WorkflowError(f"External inputs unavailable after preparation; a fresh attempt is required: {error}") from error
        if baseline["inputs"] != observed:
            raise WorkflowError("External inputs changed after preparation; a fresh attempt is required")

    def prepare(self, attempt):
        path = self.attempt_dir(attempt) / "inputs.json"
        if _files.exists(path):
            self.check_inputs(attempt)
            return
        observed = inputs.observe(attempt["structure"]["inputs"], self.locations)
        self.validate_roots()
        record = _record("inputs", **self.identity(attempt), preparation=attempt["preparation"], inputs=observed,
                         admission=getattr(self, "runtime_admission", None))
        try:
            _files.publish(path, record, replace=False)
        except FileExistsError:
            self.check_inputs(attempt)

    def checked_target(self, attempt, local, *, check_files=True):
        record = self.read(attempt, f"targets/{local}.json", "target-success",
                           execution=attempt["executions"][local], target=local)
        paths = attempt["structure"]["targets"][local]["outputs"]
        if record is None or not _valid_metadata(record.get("outputs"), paths):
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

    def initialize(self, observation, result_dir, *, tracking, managed_tmpdir):
        self.validate_roots()
        if self.owner is None:
            owner = uuid4().hex
            for path in self.locations.values():
                _files.mkdir(path)
            self.owner = _record("owner", owner=owner, task=None, working_dir=str(self.working_dir),
                                 locations={key: str(path) for key, path in self.locations.items()},
                                 root_identity={key: _files.identity(path) for key, path in self.locations.items()})
            _files.publish(self.owner_path, self.owner)
        self.current(observation.name, result_dir)
        attempt_id, operation, preparation = uuid4().hex, uuid4().hex, uuid4().hex
        executions = {local: uuid4().hex for local in observation.structure["targets"]}
        jobs = {local: f"{observation.name}__{local}__{execution}" for local, execution in executions.items()}
        jobs["gwflow_prepare"] = f"{observation.name}__gwflow_prepare__{preparation}"
        jobs["gwflow_complete"] = f"{observation.name}__gwflow_complete__{operation}"
        fingerprint = _fingerprint(observation.structure, observation.commands, tracking)
        attempt = _record("attempt", owner=self.owner["owner"], task=observation.name, attempt=attempt_id,
                          operation=operation, preparation=preparation, executions=executions, jobs=jobs, result_dir=result_dir,
                          structure=observation.structure, commands=observation.commands,
                          command_tracking=bool(tracking), fingerprint=fingerprint, managed_tmpdir=managed_tmpdir)
        _files.publish(self.attempt_dir(attempt) / "attempt.json", attempt)
        _files.publish(self._task_dir(observation.name) / "current.json",
                       _record("current", **self.identity(attempt)))
        _files.mkdir(self.workspace(attempt))
        self.publish(attempt, "ready.json", "ready")
        return attempt
