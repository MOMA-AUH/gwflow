"""Measure installed exact-Submission fallback; timings are descriptive only."""

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import tempfile
import time

from gwf.backends.local import LocalStatus
from gwflow import _files, _local_termination, _observations
from gwflow._local_termination import TerminationRecords

from test_local_termination_records import LookupWork


def measure(history, lookups):
    with tempfile.TemporaryDirectory() as root:
        records = TerminationRecords(root)
        records.root.mkdir(parents=True)
        records.path.write_text(json.dumps({"version": 1, "submissions": {
            f"submission-{number}": "COMPLETED" for number in range(history)}}))

        def lookup():
            with _observations.planning_pass():
                for number in range(lookups):
                    state = TerminationRecords(root).read(f"submission-{number % history}")
                    if state != LocalStatus.COMPLETED:
                        raise AssertionError(state)

        started = time.perf_counter()
        lookup()
        elapsed = time.perf_counter() - started
        with LookupWork() as counts:
            lookup()
        return {"history": history, "lookups": lookups,
                "seconds_uninstrumented": elapsed, "operations": dict(counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    evidence = {
        "python": platform.python_version(),
        "versions": {name: version(name) for name in ("gwflow", "gwf")},
        "installed_source_sha256": {
            module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (_files, _local_termination, _observations)},
        "measurements": [measure(history, lookups) for history, lookups in (
            (1000, 1000), (10000, 1000), (1000, 10000), (10000, 10000))],
        "scope": "Exact-Submission lookup in one observation; not total CLI latency. "
                 "Timings and operation counts are separate runs; no timing pass threshold.",
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
