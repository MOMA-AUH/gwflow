"""Non-following, directory-relative operations for owned managed storage."""

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from gwf.exceptions import WorkflowError

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


def identity(path):
    with directory(path) as fd:
        info = os.fstat(fd)
        return {"device": info.st_dev, "inode": info.st_ino}


def exists(path):
    try:
        with directory(Path(path).parent) as fd:
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
            if create:
                sync_directory(parent)
        finally:
            os.close(fd)


def metadata(root, filenames, *, sync=False):
    result = {}
    for filename in filenames:
        with regular_file(root, filename) as fd:
            info = os.fstat(fd)
            result[filename] = {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
            if sync:
                os.fsync(fd)
    return result


def read_json(path):
    path = Path(path)
    try:
        with directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd) as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                value = json.load(stream)
                return value if isinstance(value, dict) else None
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


def commit_directory(source, destination):
    source, destination = Path(source), Path(destination)
    with directory(source) as staged:
        sync_directory(staged)
    with directory(source.parent) as src, directory(destination.parent, create=True) as dst:
        # Refuse existing destinations; never merge file sets or adopt them.
        try:
            os.stat(destination.name, dir_fd=dst, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise WorkflowError(f"Managed destination already exists: {destination}")
        os.rename(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst)
        sync_directory(src)
        sync_directory(dst)
