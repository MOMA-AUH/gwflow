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
    """Track a directory within trusted storage; device numbers are host-local."""
    with directory(path) as fd:
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


def file_set(root):
    """List the complete regular-file set without traversing any symlinks."""
    def visit(parent, prefix):
        files = set()
        for name in os.listdir(parent):
            relative = prefix + name
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
                try:
                    files.update(visit(child, relative + "/"))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                files.add(relative)
            else:
                raise WorkflowError(f"Non-regular retained output: {relative}")
        return files
    with directory(root) as parent:
        return visit(parent, "")


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
