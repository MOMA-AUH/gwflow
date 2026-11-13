"""Measure ownership validation alone, excluding record I/O and authoring.

Run with the selected installed interpreter and --output under build/validation.
Timings support the deterministic regression bounds; they are not pass gates.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import time

import gwflow.lifecycle
from gwflow.lifecycle import Store
from test_result_ownership import OwnershipComparisons


def measure(store, count, *, growing, profile):
    recorded = [{"task": f"task{i}", "result_dir": f"samples/{i}/report"} for i in reversed(range(count))]
    def validate():
        if growing:
            # The initialization and finish-initialization boundaries each
            # validate the then-current complete recorded inventory.
            for size in range(1, count + 1):
                for _ in range(2):
                    store.validate_result_ownership({}, attempts=recorded[:size])
        else:
            store.validate_result_ownership({}, attempts=recorded)
    if profile:
        with OwnershipComparisons() as comparisons:
            validate()
        return {"tasks": count, "overlap_comparisons": comparisons.count}
    start = time.perf_counter()
    validate()
    return {"tasks": count, "seconds": time.perf_counter() - start}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory() as root:
        store = Store(root)
        evidence = {
            "python": platform.python_version(),
            "lifecycle_sha256": hashlib.sha256(Path(gwflow.lifecycle.__file__).read_bytes()).hexdigest(),
            "scope": "Real ownership validator with synthetic recorded inventory; excludes record I/O, authoring and scheduling",
            "counts": "Calls to the overlap predicate; excludes builtin component sorting",
            "complete_counts": [measure(store, n, growing=False, profile=True) for n in (64, 128, 256)],
            "growing_counts": [measure(store, n, growing=True, profile=True) for n in (16, 32, 64)],
            "complete_timings": [measure(store, n, growing=False, profile=False) for n in (1000, 2000)],
            "growing_timings": [measure(store, n, growing=True, profile=False) for n in (64, 128, 256)],
        }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
