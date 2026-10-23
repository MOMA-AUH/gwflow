"""Test-only consolidated storage setup; no runtime conversion is enabled."""

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
