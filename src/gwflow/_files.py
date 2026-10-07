"""Non-following, directory-relative operations for owned managed storage."""

from collections import OrderedDict
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from gwf.exceptions import WorkflowError

from . import _observations
from .workflow import relative_path


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def sync_directory(fd):
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise


@contextmanager
def directory(path, *, create=False):
    """Open every component without following links, including the final one."""
    path = Path(path)
    if not path.is_absolute():
        raise WorkflowError(f"Managed location must be absolute: {path}")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if part in (".", ".."):
                raise WorkflowError(f"Invalid managed directory: {path}")
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                    sync_directory(fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise WorkflowError(f"Managed directory contains a symlink or non-directory: {path}") from exc
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class _ReadDirectories:
    """Bounded handles, with current no-follow ancestry checked on every use."""

    limit = 64

    def __init__(self):
        self.handles = OrderedDict()
        self.ancestry = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for fd, _ in self.handles.values():
            os.close(fd)
        self.handles.clear()
        self.ancestry.clear()

    def borrow(self, path):
        if path not in self.handles:
            return self.open(path)
        self.handles.move_to_end(path)
        return self.handles[path][0]

    def open(self, path):
        parent = None
        if path not in self.ancestry:
            self.ancestry[path] = [(p, p.name or "/") for p in (*reversed(path.parents), path)]
        for current, part in self.ancestry[path]:
            if current in self.handles:
                fd, identity = self.handles[current]
                info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                    raise WorkflowError(f"Managed directory changed while observing evidence: {current}")
                self.handles.move_to_end(current)
            else:
                try:
                    fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise WorkflowError(f"Managed directory contains a symlink or non-directory: {path}") from exc
                    raise
                try:
                    info = os.fstat(fd)
                    self.handles[current] = fd, (info.st_dev, info.st_ino)
                except BaseException:
                    os.close(fd)
                    raise
                if len(self.handles) > self.limit:
                    _, (old, _) = self.handles.popitem(last=False)
                    os.close(old)
            parent = fd
        return parent


@contextmanager
def _read_directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkflowError(f"Invalid managed directory: {path}")
    reader = _observations.resource("directories", _ReadDirectories)
    if reader is None:
        with directory(path) as fd:
            yield fd
    elif len(path.parts) > reader.limit:
        # Very deep paths use bounded fresh traversal rather than retaining
        # their entire ancestry. Check the configured pathname after reading.
        with directory(path) as fd:
            expected = os.fstat(fd)
            yield fd
            with directory(path) as current:
                observed = os.fstat(current)
                if (expected.st_dev, expected.st_ino) != (observed.st_dev, observed.st_ino):
                    raise WorkflowError(f"Managed directory changed while observing evidence: {path}")
    else:
        fd = reader.borrow(path)
        yield fd
        # A retained handle may now name a displaced directory. Recheck every
        # link from / before accepting evidence read through that handle.
        reader.open(path)


def identity(path):
    """Track a directory within trusted storage; device numbers are host-local."""
    with _read_directory(path) as fd:
        info = os.fstat(fd)
        return {"inode": info.st_ino}


def valid_identity(value):
    return (isinstance(value, dict) and set(value) == {"inode"}
            and type(value["inode"]) is int and value["inode"] >= 0)


def same_directory(left, right):
    """Only matching current-format identities establish directory ownership."""
    return valid_identity(left) and valid_identity(right) and left == right


def exists(path):
    try:
        with _read_directory(Path(path).parent) as fd:
            os.stat(Path(path).name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def mkdir(path):
    with directory(path, create=True):
        pass


@contextmanager
def regular_file(root, relative, *, create=False):
    path = Path(root) / relative_path(relative)
    with directory(path.parent, create=create) as parent:
        with _regular_at(parent, path, create=create) as fd:
            yield fd
            if create:
                sync_directory(parent)


@contextmanager
def _regular_at(parent, path, *, create=False):
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= (os.O_WRONLY | os.O_CREAT | os.O_EXCL) if create else os.O_RDONLY | os.O_NONBLOCK
    try:
        fd = os.open(path.name, flags, 0o600, dir_fd=parent)
    except OSError as exc:
        raise WorkflowError(f"Cannot open regular managed file {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkflowError(f"Managed output must be a regular file: {path}")
        yield fd
    finally:
        os.close(fd)


def _signature(info):
    # Device numbers identify observations only in this process, never owners.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _unchanged(path, expected, observed):
    if expected is not None and _signature(expected) != _signature(observed):
        raise WorkflowError(f"Managed file changed while observing evidence: {path}")


def _observed_file(path, kind, read):
    with _read_directory(path.parent) as parent:
        if not _observations.active():
            return read(parent, None)
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            # Preserve the reader's missing-file/error semantics and permit
            # publication after this absent observation to be seen immediately.
            return read(parent, None)
        return _observations.reuse(kind, (str(path), _signature(info)), lambda: read(parent, info))


def metadata(root, filenames, *, sync=False):
    result = {}
    for filename in filenames:
        path = Path(root) / relative_path(filename)
        def read(parent, expected):
            with _regular_at(parent, path) as fd:
                info = os.fstat(fd)
                _unchanged(path, expected, info)
                if sync:
                    os.fsync(fd)
                return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
        if sync:
            # Durability checks must always reach the filesystem.
            with directory(path.parent) as parent:
                result[filename] = read(parent, None)
        else:
            result[filename] = _observed_file(path, "managed-metadata", read)
    return result


def file_set(root):
    """List regular files with at most two transient directory descriptors."""
    files, pending = set(), [()]
    with _read_directory(root) as anchor:
        while pending:
            parts = pending.pop()
            parent = anchor
            try:
                # Reopen relative to the pinned root; retaining a recursive
                # stack would make descriptor use grow with output depth.
                for part in parts:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
                    if parent != anchor:
                        os.close(parent)
                    parent = child
                for name in os.listdir(parent):
                    relative = (*parts, name)
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(relative)
                    elif stat.S_ISREG(info.st_mode):
                        files.add("/".join(relative))
                    else:
                        raise WorkflowError(f"Non-regular retained output: {'/'.join(relative)}")
            finally:
                if parent != anchor:
                    os.close(parent)
    return files


def read_json(path):
    path = Path(path)
    def read(parent, expected):
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            _unchanged(path, expected, info)
            if not stat.S_ISREG(info.st_mode):
                return None
            value = json.load(stream)
            return value if isinstance(value, dict) else None
    try:
        return _observed_file(path, "json", read)
    except (FileNotFoundError, ValueError, UnicodeError):
        return None
    except OSError as exc:
        raise WorkflowError(f"Cannot read managed evidence {path}: {exc}") from exc


def publish(path, record, *, replace=True):
    path = Path(path)
    with directory(path.parent, create=True) as parent:
        temporary = ".pending-" + uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            sync_directory(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def commit_directory(source, destination, *, expected=None):
    source, destination = Path(source), Path(destination)
    with directory(source) as staged:
        info = os.fstat(staged)
        if expected is not None and not same_directory({"inode": info.st_ino}, expected):
            raise WorkflowError(f"Managed staging ownership changed: {source}")
        sync_directory(staged)
    with directory(source.parent) as src, directory(destination.parent, create=True) as dst:
        # Refuse existing destinations; never merge file sets or adopt them.
        try:
            os.stat(destination.name, dir_fd=dst, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise WorkflowError(f"Managed destination already exists: {destination}")
        current = os.stat(source.name, dir_fd=src, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise WorkflowError(f"Managed staging changed before commit: {source}")
        os.rename(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst)
        sync_directory(src)
        sync_directory(dst)


def remove_directory(path, expected):
    """Remove an owned directory by descriptor, never following child links."""
    path = Path(path)
    with directory(path.parent) as parent:
        try:
            child = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return
        except OSError as error:
            raise WorkflowError(f"Cannot remove managed directory {path}: {error}") from error
        try:
            info = os.fstat(child)
            if not same_directory({"inode": info.st_ino}, expected):
                raise WorkflowError(f"Managed directory ownership changed before removal: {path}")
            _remove_contents(child)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise WorkflowError(f"Managed directory changed during removal: {path}")
            os.rmdir(path.name, dir_fd=parent)
            sync_directory(parent)
        finally:
            os.close(child)


def _remove_contents(parent):
    for name in os.listdir(parent):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise WorkflowError("Managed child directory changed before removal")
                _remove_contents(child)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise WorkflowError("Managed child directory changed during removal")
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
        sync_directory(parent)
