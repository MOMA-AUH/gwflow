"""Storage-transition setup and authoritative evidence corruption for tests."""

import json
import shutil

from gwflow import _files


def consolidate(records, root, *, phase="compacted", remove=True):
    with records.access(write=True):
        path = root / "record-state.json"
        state = json.loads(path.read_text())
        collection = {str(path.relative_to(root)): json.loads(path.read_text())
                      for path in root.rglob("*.json") if path.name != "record-state.json"}
        state.update(phase=phase, revision=state["revision"] + 1, records=collection)
        _files.publish(path, state)
        if remove:
            for child in root.iterdir():
                if child.name in {"record-state.json", "records.lock"}:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    return state


def record_path(work, task, pattern):
    """Locate selected logical evidence for corruption tests in either phase."""
    from fnmatch import fnmatch

    task_root = next((work / '.gwf/gwflow').glob('owners/*/tasks/' + task))
    current = json.loads((task_root / 'current.json').read_text())
    root = task_root / 'attempts' / current['attempt']
    state = json.loads((root / 'record-state.json').read_text())
    names = (state['records'] if state['phase'] != 'individual'
             else (str(path.relative_to(root)) for path in root.rglob('*.json')))
    return root / next(name for name in names if fnmatch(name, pattern))


def authority(path):
    for root in path.parents:
        document = root / 'record-state.json'
        if document.is_file():
            return document, json.loads(document.read_text()), str(path.relative_to(root))
    raise AssertionError(f'No record authority for {path}')


def read_record(path):
    document, state, name = authority(path)
    return json.loads(path.read_text()) if state['phase'] == 'individual' else state['records'].get(name)


def write_record(path, value):
    """Inject damage into actual authoritative evidence, retaining its phase."""
    document, state, name = authority(path)
    if state['phase'] == 'individual':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    else:
        state['records'][name] = value
        state['revision'] += 1
        _files.publish(document, state)


def remove_record(path):
    document, state, name = authority(path)
    if state['phase'] == 'individual':
        path.unlink()
    else:
        del state['records'][name]
        state['revision'] += 1
        _files.publish(document, state)


def restore_individual(work, task):
    """Prepare physical-file substitution faults at the restoration seam."""
    from gwflow.lifecycle import Store

    store = Store(work)
    store.records(store.recorded_current(task)).restore()
