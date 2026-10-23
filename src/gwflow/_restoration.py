"""Reconstruct individual records while embedded evidence remains authoritative.

A private scratch collection is never adopted on retry. Its verified inode and
content claims are durable before any entry moves to a logical record pathname.
An interrupted reconstruction is discarded using those claims and rebuilt from
current authority, including writes received since the interruption.
"""

from contextlib import contextmanager
import os
import re
import stat
from uuid import uuid4

from gwf.exceptions import WorkflowError

from . import _files
from ._compaction import _durable, _inventory, _owned_record, _PENDING, _publish, _remove
from ._records import _Access


_SCRATCH = re.compile(r'\.restoring-[0-9a-f]{32}')


@contextmanager
def _scratch(records, state, name, expected=None):
    info = os.stat(name, dir_fd=state.directory, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or expected is not None and expected != info.st_ino):
        raise WorkflowError('Restoration scratch ownership changed')
    fd = os.open(name, _files._DIRECTORY_FLAGS, dir_fd=state.directory)
    child = _Access(state.document, fd, state.lock)
    try:
        _files._unchanged(name, info, os.fstat(fd))
        yield child
        current = os.stat(name, dir_fd=state.directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise WorkflowError('Restoration scratch directory changed')
    finally:
        child.close()
        os.close(fd)


def _check_claims(observed, expected):
    if not isinstance(expected, dict):
        raise WorkflowError('Missing restoration ownership claims')
    for name, claim in observed.items():
        if not _PENDING.fullmatch(name.split('/')[-1]) and expected.get(name) != claim:
            raise WorkflowError(f'Unowned restoration metadata: {name}')


def _discard_scratch(records, state, name, expected=None):
    with _scratch(records, state, name, expected) as child:
        claims, _ = _inventory(records, child)
        _remove(records, child, claims, verify=lambda: records._verify_access(state))
    os.rmdir(name, dir_fd=state.directory)
    _files.sync_directory(state.directory)


def _reset_reconstruction(records, state):
    """Discard only claimed canonical entries and validated private scratch."""
    restoration = state.document.get('restoration')
    if restoration is not None:
        if (not isinstance(restoration, dict)
                or not isinstance(restoration.get('source'), str)
                or not _SCRATCH.fullmatch(restoration['source'])
                or type(restoration.get('inode')) is not int
                or not isinstance(restoration.get('claims'), dict)):
            raise WorkflowError('Malformed restoration ownership')
    scratch = [name for name in os.listdir(state.directory) if _SCRATCH.fullmatch(name)]
    claims, _ = _inventory(records, state, exclude=scratch)
    expected = restoration['claims'] if restoration else state.document.get('redundant', {})
    _check_claims(claims, expected)
    # Validate every scratch collection before deleting any canonical record.
    for name in scratch:
        owned = restoration is not None and name == restoration['source']
        with _scratch(records, state, name, restoration['inode'] if owned else None) as child:
            staged, _ = _inventory(records, child)
            if owned:
                _check_claims(staged, expected)
    _remove(records, state, claims)
    for name in scratch:
        owned = restoration is not None and name == restoration['source']
        _discard_scratch(records, state, name, restoration['inode'] if owned else None)
    _publish(records, state, restoration=None, redundant={})


def _sync_collection(records, state, claims):
    for name, claim in claims.items():
        with state.parent(name + '/child' if claim.get('directory') else name) as parent:
            if claim.get('directory'):
                _files.sync_directory(parent)
            else:
                with _files._regular_at(parent, records._root / name) as fd:
                    os.fsync(fd)
    _files.sync_directory(state.directory)


def restore(records, state):
    try:
        # Even an individual document may be visible after an interrupted
        # authority-switch fsync. Repair admission must wait for durability.
        _durable(records, state)
        if state.document['phase'] == 'individual':
            return
        for name, value in state.document['records'].items():
            _owned_record(records, name, value)
        if state.document['phase'] != 'restoring':
            _publish(records, state, phase='restoring', restoration=None)
        _reset_reconstruction(records, state)
        source = '.restoring-' + uuid4().hex
        os.mkdir(source, mode=0o700, dir_fd=state.directory)
        _files.sync_directory(state.directory)
        with _scratch(records, state, source) as child:
            for name, value in state.document['records'].items():
                _files.publish(records._root / name, value, replace=False,
                               parent_directory=child.parent(name, create=True))
            claims, collection = _inventory(records, child)
            if collection != state.document['records']:
                raise WorkflowError('Restored collection differs from authoritative records')
            _sync_collection(records, child, claims)
            _publish(records, state, restoration={
                'source': source, 'inode': os.fstat(child.directory).st_ino, 'claims': claims,
            })
            # No canonical path is populated before its ownership is durable.
            for name in sorted(os.listdir(child.directory)):
                if name in os.listdir(state.directory):
                    raise WorkflowError(f'Unowned restoration destination: {name}')
                os.rename(name, name, src_dir_fd=child.directory, dst_dir_fd=state.directory)
                _files.sync_directory(child.directory)
                _files.sync_directory(state.directory)
            child.close()
        os.rmdir(source, dir_fd=state.directory)
        _files.sync_directory(state.directory)
        observed, collection = _inventory(records, state)
        if observed != claims or collection != state.document['records']:
            raise WorkflowError('Restored collection changed before authority publication')
        _sync_collection(records, state, observed)
        _publish(records, state, phase='individual', records=None, restoration=None, redundant={})
    except (OSError, WorkflowError) as error:
        raise WorkflowError(f'Record restoration failed: {error}') from error
