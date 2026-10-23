"""Attempt-scoped authority and coordination for logical lifecycle records.

The persistent lock is never replaced. A state document selects loose records
or embeds the authoritative collection. Sessions are short: callers must leave
before inspecting another attempt, running commands, copying data or waiting
for a scheduler. Metadata child handles live only for the duration of a locked session.
"""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import fcntl
import json
import os
import re
import stat

from gwf.exceptions import WorkflowError
from gwf.utils import is_valid_name

from . import _files, _observations
from .workflow import relative_path


_STATE = "record-state.json"
_LOCK = "records.lock"
_PHASES = {"individual", "consolidating", "compacted", "restoring"}
_OPERATION_RECORDS = {"staging.json", "transfer.json", "manifest.json", "installed.json",
                      "completion.json", "repair.json", "installation.json"}
_current = ContextVar("gwflow_attempt_records", default=None)


def _uuid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _logical_name(name):
    if (not isinstance(name, str) or relative_path(name) != name
            or name.split("/")[0] in {_STATE, _LOCK}
            or any(part.startswith(".pending-") for part in name.split("/"))):
        raise WorkflowError(f"Invalid logical attempt record name: {name!r}")
    return name


class _Documents:
    """Bounded decoded documents, without retained file or directory handles."""

    def __init__(self):
        self.values = OrderedDict()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.values.clear()

    def read(self, path, signature, load):
        previous = self.values.pop(path, None)
        value = previous[1] if previous is not None and previous[0] == signature else load()
        self.values[path] = (signature, value)
        if len(self.values) > 8:
            self.values.popitem(last=False)
        return value


class AttemptRecords:
    def __init__(self, task_directory, attempt):
        self._identity = {key: attempt[key] for key in ("owner", "task", "attempt")}
        if (not _uuid(self._identity["owner"]) or not _uuid(self._identity["attempt"])
                or not isinstance(self._identity["task"], str) or not is_valid_name(self._identity["task"])):
            raise WorkflowError("Invalid attempt record identity")
        self._root = task_directory / "attempts" / attempt["attempt"]
        self._operation = attempt.get("operation")

    def _name(self, name):
        if name in _OPERATION_RECORDS:
            if not _uuid(self._operation):
                raise WorkflowError("Missing attempt operation identity")
            name = f"operations/{self._operation}/{name}"
        return _logical_name(name)

    def initialize(self):
        """Allocate a new namespace; never adopt a missing state or lock."""
        with _files.directory(self._root.parent, create=True) as parent:
            os.mkdir(self._root.name, dir_fd=parent)
            _files.sync_directory(parent)
        with _files.directory(self._root) as directory:
            fd = os.open(_LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.fsync(fd)
                _files.publish(self._root / _STATE, {
                    "kind": "gwflow.record-state", "schema": 1, **self._identity,
                    "revision": 0, "phase": "individual", "records": None,
                    "directory": {"inode": os.fstat(directory).st_ino},
                    "lock": {"inode": os.fstat(fd).st_ino},
                }, replace=False)
            finally:
                os.close(fd)

    def _load(self, directory, lock):
        info = os.stat(_STATE, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkflowError("Attempt record-state must be a regular, unlinked file")

        def decode():
            with _files._regular_at(directory, self._root / _STATE) as fd:
                if _files._signature(os.fstat(fd)) != _files._signature(info):
                    raise WorkflowError("Attempt record-state changed while opening")
                with os.fdopen(fd, closefd=False) as stream:
                    state = json.load(stream)
            if (not isinstance(state, dict) or state.get("kind") != "gwflow.record-state"
                    or type(state.get("schema")) is not int or state["schema"] != 1
                    or type(state.get("revision")) is not int or state["revision"] < 0
                    or not isinstance(state.get("phase"), str) or state["phase"] not in _PHASES):
                raise WorkflowError("Malformed or unsupported attempt record-state")
            records = state.get("records")
            if state["phase"] == "individual":
                if "records" not in state or records is not None:
                    raise WorkflowError("Individual record-state must select loose records")
            else:
                if not isinstance(records, dict):
                    raise WorkflowError("Missing authoritative attempt record collection")
                for name, record in records.items():
                    _logical_name(name)
                    if not isinstance(record, dict):
                        raise WorkflowError(f"Malformed authoritative attempt record: {name}")
                    parts = name.split("/")
                    if any("/".join(parts[:index]) in records for index in range(1, len(parts))):
                        raise WorkflowError("Conflicting authoritative attempt record names")
            return state

        cache = _observations.resource("attempt-documents", _Documents)
        state = decode() if cache is None else cache.read(self._root, _files._signature(info), decode)
        if (any(state.get(key) != value for key, value in self._identity.items())
                or state.get("directory") != {"inode": os.fstat(directory).st_ino}
                or state.get("lock") != {"inode": os.fstat(lock).st_ino}):
            raise WorkflowError("Attempt record-state ownership or lock identity changed")
        return state

    @contextmanager
    def access(self, *, write=False):
        """One coherent logical observation or short exclusive mutation."""
        active = _current.get()
        if active is not None:
            records, writable, state = active
            if records._root != self._root or records._identity != self._identity:
                raise WorkflowError("Cannot nest record locks across attempts")
            if write and not writable:
                raise WorkflowError("Cannot mutate a shared record observation")
            yield state
            return
        try:
            with _files._read_directory(self._root) as directory:
                fd = os.open(_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise WorkflowError("Attempt record lock must be a regular, unlinked file")
                    fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                    current = os.stat(_LOCK, dir_fd=directory, follow_symlinks=False)
                    if _files._signature(current) != _files._signature(info):
                        raise WorkflowError("Attempt record lock changed while acquiring access")
                    state = _Access(self._load(directory, fd), directory, info)
                    token = _current.set((self, write, state))
                    try:
                        yield state
                    finally:
                        _current.reset(token)
                        state.close()
                finally:
                    os.close(fd)
        except FileExistsError:
            raise
        except (OSError, ValueError, UnicodeError) as error:
            raise WorkflowError(f"Cannot access authoritative attempt records {self._root}: {error}") from error

    def read(self, name):
        name = self._name(name)
        with self.access() as state:
            if state.document["phase"] != "individual":
                return deepcopy(state.document["records"].get(name))
            # Loose metadata directories can disappear during a later storage
            # transition. Never retain their descriptors in a planning cache.
            return _files.read_json(self._root / name, parent_directory=state.parent(name))

    def publish(self, name, record, *, replace=True):
        name = self._name(name)
        with self.access(write=True) as state:
            # A held descriptor must never authorize a write through a
            # substituted pathname, even inside an explicit caller session.
            with _files.directory(self._root) as directory:
                opened, current = os.fstat(state.directory), os.fstat(directory)
                lock = os.stat(_LOCK, dir_fd=directory, follow_symlinks=False)
                if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or _files._signature(lock) != _files._signature(state.lock)):
                    raise WorkflowError("Attempt record directory or lock changed before mutation")
            document = state.document
            updated = {**document, "revision": document["revision"] + 1}
            if document["phase"] == "individual":
                _files.publish(self._root / name, record, replace=replace)
                return
            if not replace and name in document["records"]:
                raise FileExistsError(name)
            if (not isinstance(record, dict)
                    or any(key != name and (key.startswith(name + "/") or name.startswith(key + "/"))
                           for key in document["records"])):
                raise WorkflowError("Invalid or conflicting authoritative attempt record")
            updated["records"] = {**document["records"], name: record}
            _files.publish(self._root / _STATE, updated)
            state.document = updated

    def version(self):
        """A revision/file identity token for derived, observation-scoped facts."""
        with self.access() as state:
            return _files._signature(os.stat(_STATE, dir_fd=state.directory, follow_symlinks=False))

    def exists(self, name):
        """Presence is independent of whether loose evidence is readable."""
        name = self._name(name)
        with self.access() as state:
            if state.document["phase"] != "individual":
                return name in state.document["records"] or any(key.startswith(name + "/") for key in state.document["records"])
            try:
                with state.parent(name) as directory:
                    os.stat((self._root / name).name, dir_fd=directory, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False

    def names(self, namespace="", *, initializing=False):
        """List immediate logical children, retaining unexpected loose names."""
        namespace = self._name(namespace) if namespace else ""
        with self.access() as state:
            if state.document["phase"] != "individual":
                prefix = namespace + "/" if namespace else ""
                return sorted({name[len(prefix):].split("/")[0] for name in state.document["records"]
                               if name.startswith(prefix)})
            root = self._root / namespace
            if initializing:
                names = _files.initialization_entries(root)
            else:
                try:
                    with state.parent(namespace + "/child" if namespace else "child") as directory:
                        names = set(os.listdir(directory))
                except FileNotFoundError:
                    return []
            return sorted(names - {_STATE, _LOCK} if not namespace else names)


class _Access:
    """Keep child descriptors bounded and confined to one held attempt lock."""

    def __init__(self, document, directory, lock):
        self.document = document
        self.directory = directory
        self.lock = lock
        self.children = OrderedDict()

    def close(self):
        for fd, _ in self.children.values():
            os.close(fd)
        self.children.clear()

    def _parent(self, name):
        directory, parts = self.directory, []
        for part in name.split("/")[:-1]:
            parts.append(part)
            key = "/".join(parts)
            if key in self.children:
                fd, expected = self.children[key]
                current = os.stat(part, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev, current.st_ino) != expected):
                    raise WorkflowError("Managed record directory changed during observation")
                self.children.move_to_end(key)
            else:
                fd = os.open(part, _files._DIRECTORY_FLAGS, dir_fd=directory)
                info = os.fstat(fd)
                self.children[key] = fd, (info.st_dev, info.st_ino)
                if len(self.children) > 8:
                    _, (old, _) = self.children.popitem(last=False)
                    os.close(old)
            directory = fd
        return directory

    @contextmanager
    def parent(self, name):
        yield self._parent(name)
        self._parent(name)


def attempt_ids(task_directory):
    """Enumerate attempt identities without exposing their physical locations."""
    with _files.directory(task_directory / "attempts") as directory:
        return sorted(os.listdir(directory))


def observing(function):
    """Group local record reads; the wrapped function must not inspect producers."""
    @wraps(function)
    def observe(store, attempt, *args, **kwargs):
        with _observations.observation(), store.records(attempt).access():
            return function(store, attempt, *args, **kwargs)
    return observe
