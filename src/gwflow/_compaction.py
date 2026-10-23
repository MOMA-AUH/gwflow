"""Recoverable, lossless removal of redundant attempt metadata.

All operations run under the persistent attempt lock. The state document
carries both authority and deletion claims; a visible authority switch is
synchronized again on recovery before any source evidence can be removed.
"""

from collections import Counter
import errno
import hashlib
import json
import os
import re
import stat

from gwf.exceptions import WorkflowError

from . import _files


_UUID = r"[0-9a-f]{32}"
_PENDING = re.compile(r"\.pending-" + _UUID)
_ROOT_RECORDS = {'attempt.json': 'attempt', 'initialization.json': 'initialization',
                 'ready.json': 'ready', 'inputs.json': 'inputs', 'cleanup.json': 'cleanup'}
_GROUP_RECORDS = {
    'operations': {'staging.json': 'staging-directory', 'transfer.json': 'transfer',
                   'manifest.json': 'manifest', 'installed.json': 'installed',
                   'completion.json': 'completion', 'repair.json': 'repair',
                   'installation.json': 'installation'},
    'executions': {'definition.json': 'execution', 'success.json': 'target-success'},
    'admissions': {'intent.json': 'submission-intent', 'ack.json': 'submission-ack',
                   'outcome.json': 'job-outcome', 'rejected.json': 'submission-rejected'},
    'recoveries': {'recovery.json': 'operator-recovery'},
}


def _kind(name):
    parts = name.split('/')
    if len(parts) == 1:
        return _ROOT_RECORDS.get(name)
    if len(parts) == 3 and re.fullmatch(_UUID, parts[1]):
        if parts[0] == 'submissions' and parts[2].endswith('-intent.json'):
            from gwf.utils import is_valid_name
            if is_valid_name(parts[2][:-len('-intent.json')]):
                return 'submission-intent'
        return _GROUP_RECORDS.get(parts[0], {}).get(parts[2])
    return None


def _directory(name):
    parts = name.split('/')
    return (parts[0] in {*_GROUP_RECORDS, 'submissions'}
            and (len(parts) == 1 or len(parts) == 2 and re.fullmatch(_UUID, parts[1])))


def _owned_record(records, name, value):
    kind = _kind(name)
    if (kind is None or not isinstance(value, dict)
            or value.get('kind') != 'gwflow.' + kind
            or type(value.get('schema')) is not int or value['schema'] != 1
            or any(value.get(key) != item for key, item in records._identity.items())):
        raise WorkflowError(f'Unsupported record ownership during compaction: {name}')


def _inventory(records, state):
    """Validate the entire physical collection before performing any deletion."""
    claims, collection, pending, links = {}, {}, [''], {}
    while pending:
        namespace = pending.pop()
        with state.parent(namespace + '/child' if namespace else 'child') as parent:
            for leaf in sorted(os.listdir(parent)):
                if not namespace and leaf in {'record-state.json', 'records.lock'}:
                    continue
                name = namespace + '/' + leaf if namespace else leaf
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if info.st_uid != os.getuid():
                    raise WorkflowError(f'Unsupported metadata owner during compaction: {name}')
                if stat.S_ISDIR(info.st_mode) and _directory(name):
                    claims[name] = {'inode': info.st_ino, 'directory': True}
                    pending.append(name)
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in (1, 2)
                        or not (_PENDING.fullmatch(leaf) or _kind(name))):
                    raise WorkflowError(f'Unsafe or unexpected metadata during compaction: {name}')
                with _files._regular_at(parent, records._root / name) as fd:
                    _files._unchanged(name, info, os.fstat(fd))
                    with os.fdopen(fd, 'rb', closefd=False) as stream:
                        data = stream.read()
                    _files._unchanged(name, info, os.fstat(fd))
                claims[name] = {'inode': info.st_ino, 'sha256': hashlib.sha256(data).hexdigest()}
                links.setdefault((info.st_dev, info.st_ino, info.st_nlink), []).append(name)
                if not _PENDING.fullmatch(leaf):
                    try:
                        value = json.loads(data)
                    except (ValueError, UnicodeError) as error:
                        raise WorkflowError(f'Malformed compaction source: {name}') from error
                    _owned_record(records, name, value)
                    collection[name] = value
    for (_, _, count), names in links.items():
        # Exclusive publication links the private temporary to its final name
        # before unlinking it. Support exactly that owned pair, never aliases
        # outside the attempt or two logical records sharing one inode.
        if (len(names) != count or count == 2
                and sum(bool(_PENDING.fullmatch(name.split('/')[-1])) for name in names) != 1):
            raise WorkflowError(f'Unsupported metadata hard links during compaction: {names}')
    return claims, collection


def _publish(attempt_records, state, **fields):
    attempt_records._verify_access(state)
    updated = {**state.document, 'revision': state.document['revision'] + 1, **fields}
    before = os.stat('record-state.json', dir_fd=state.directory, follow_symlinks=False)
    try:
        _files.publish(attempt_records._root / 'record-state.json', updated,
                       parent_directory=state.parent('record-state.json'))
    except (OSError, WorkflowError) as error:
        after = os.stat('record-state.json', dir_fd=state.directory, follow_symlinks=False)
        boundary = ('after replacement; synchronization uncertain' if before.st_ino != after.st_ino
                    else 'before replacement')
        raise WorkflowError(f'Record-state publication failed {boundary}: {error}') from error
    state.document = updated
    attempt_records._verify_access(state)


def _durable(records, state):
    # A previous replacement may be visible despite failed directory fsync.
    with _files._regular_at(state.directory, records._root / 'record-state.json') as fd:
        with os.fdopen(fd, closefd=False) as stream:
            if json.load(stream) != state.document:
                raise WorkflowError('Authoritative compaction document changed before synchronization')
        os.fsync(fd)
    _files.sync_directory(state.directory)


def _remove(records, state, claims):
    # Children precede parents; validate names/identities again at each boundary.
    links = Counter(claim['inode'] for claim in claims.values() if not claim.get('directory'))
    for name in sorted(claims, key=lambda name: (-name.count('/'), name)):
        records._verify_access(state)
        expected = claims[name]
        with state.parent(name) as parent:
            info = os.stat(name.split('/')[-1], dir_fd=parent, follow_symlinks=False)
            if info.st_ino != expected['inode']:
                raise WorkflowError(f'Redundant metadata identity changed: {name}')
            leaf = name.split('/')[-1]
            if expected.get('directory'):
                if not stat.S_ISDIR(info.st_mode):
                    raise WorkflowError(f'Redundant metadata directory changed: {name}')
                # rmdir refuses any unexpected content; never recursively erase.
                try:
                    os.rmdir(leaf, dir_fd=parent)
                except OSError as error:
                    raise WorkflowError(f'Redundant directory removal failed: {name}: {error}') from error
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != links[info.st_ino]:
                    raise WorkflowError(f'Redundant record changed: {name}')
                with _files._regular_at(parent, records._root / name) as fd:
                    with os.fdopen(fd, 'rb', closefd=False) as stream:
                        digest = hashlib.sha256(stream.read()).hexdigest()
                    _files._unchanged(name, info, os.fstat(fd))
                if digest != expected['sha256']:
                    raise WorkflowError(f'Redundant record contents changed: {name}')
                _files._unchanged(name, info, os.stat(leaf, dir_fd=parent, follow_symlinks=False))
                try:
                    os.unlink(leaf, dir_fd=parent)
                except OSError as error:
                    raise WorkflowError(f'Redundant-file deletion failed: {name}: {error}') from error
            _files.sync_directory(parent)
        if not expected.get('directory'):
            links[expected['inode']] -= 1
        # Drop descriptors for directories that may just have been removed.
        state.close()


def compact(records, state, outcome_name, outcome):
    stage = 'candidate validation'
    try:
        _owned_record(records, outcome_name, outcome)
        document = state.document
        if document['phase'] == 'restoring':
            raise WorkflowError('Cannot compact an attempt while restoring individual records')
        claims, collection = _inventory(records, state)
        if document['phase'] == 'individual':
            # A second fresh read validates the complete candidate and deletion
            # claims against their sources before switching authority.
            if _inventory(records, state) != (claims, collection):
                raise WorkflowError('Compaction source collection changed during validation')
            stage = 'consolidating publication or synchronization'
            collection = {**collection, outcome_name: {**outcome, 'state': 'compacting'}}
            _publish(records, state, phase='consolidating', records=collection, redundant=claims)
        else:
            for name, value in document['records'].items():
                _owned_record(records, name, value)
            source_claims = document.get('redundant', {})
            if not isinstance(source_claims, dict):
                raise WorkflowError('Malformed redundant metadata ownership claims')
            for name, claim in claims.items():
                # Publication temporaries can be left after any atomic write,
                # including late acknowledgements. They are never evidence.
                if not _PENDING.fullmatch(name.split('/')[-1]) and source_claims.get(name) != claim:
                    raise WorkflowError(f'Unrecognized redundant metadata ownership: {name}')
        stage = 'replacement synchronization'
        _durable(records, state)
        # Also synchronize surviving directories: an earlier deletion may have
        # succeeded without its following fsync. This precedes further deletion.
        for name in claims:
            if claims[name].get('directory'):
                with state.parent(name + '/child') as directory:
                    _files.sync_directory(directory)
        _files.sync_directory(state.directory)
        stage = 'redundant record deletion or directory removal'
        _remove(records, state, claims)
        if set(os.listdir(state.directory)) != {'record-state.json', 'records.lock'}:
            raise WorkflowError('Unexpected metadata remains after compaction')
        stage = 'final publication or synchronization'
        collection = {**state.document['records'], outcome_name: outcome}
        _publish(records, state, phase='compacted', records=collection, redundant={})
        # Atomic publication removes its own temporary after the replace fsync.
        # Establish durability of that removal before returning success.
        _files.sync_directory(state.directory)
    except (OSError, WorkflowError) as error:
        cause = error
        while cause.__cause__ is not None:
            cause = cause.__cause__
        full = ' (filesystem full)' if isinstance(cause, OSError) and cause.errno in (errno.ENOSPC, errno.EDQUOT) else ''
        raise WorkflowError(f'Record compaction failed during {stage}{full}: {error}') from error
