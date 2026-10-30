"""Partition installed-package tests and verify complete, unchanged-runtime coverage."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from conda_runtime import capture, restore, run


IDENTITY_FIELDS = ("schema_version", "platform", "source_commit", "artifact", "manifest", "fixtures", "fixture_versions")


def runtime_identity(record: dict) -> dict:
    return {field: record[field] for field in IDENTITY_FIELDS}


def check_runtime(conda: str, bundle: Path, prefix: Path, source: Path) -> dict:
    expected = json.loads((bundle / "runtime.json").read_text())
    with tempfile.TemporaryDirectory(prefix="gwflow-ci-capture-") as directory:
        output = Path(directory) / "capture"
        capture(conda, prefix, bundle / expected["artifact"]["filename"], source, output)
        actual = json.loads((output / "runtime.json").read_text())
    if runtime_identity(actual) != runtime_identity(expected):
        raise ValueError("CI runtime or source differs from the shared build")
    return runtime_identity(actual)


def install(conda: str, bundle: Path, prefix: Path, source: Path) -> None:
    restore(conda, bundle, prefix)
    run(str(prefix / "bin/python"), "-m", "pip", "install", "--no-deps",
        *(str(source / "examples/packaged/packages" / name) for name in ("summary-task", "report-task")),
        show_output=True)
    check_runtime(conda, bundle, prefix, source)


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def partition(tests: list, index: int, count: int) -> list:
    if count < 1 or not 0 <= index < count:
        raise ValueError("Shard index must be between zero and count minus one")
    # Stable identities spread tests from expensive modules across all runners.
    return [test for test in tests if int.from_bytes(hashlib.sha256(test.id().encode()).digest()[:8]) % count == index]


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append({"test": test.id(), "seconds": round(time.monotonic() - self.started, 3)})
        super().stopTest(test)


def verify_records(tests: list, records: list[dict], identity: dict, count: int) -> None:
    if len(records) != count or {record["index"] for record in records} != set(range(count)):
        raise ValueError("Missing or duplicate CI shards")
    for record in records:
        expected = Counter(test.id() for test in partition(tests, record["index"], count))
        if (record["status"] != "passed" or record["count"] != count or record["runtime"] != identity
                or record["run"] != sum(expected.values()) or Counter(record["tests"]) != expected
                or record["skipped"] != 0):
            raise ValueError(f"Incomplete, failed, or mismatched CI shard: {record['index']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "run", "verify"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--conda", default="conda")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--records", type=Path)
    args = parser.parse_args()
    args.bundle, args.prefix, args.source = args.bundle.resolve(), args.prefix.resolve(), args.source.resolve()
    try:
        if args.command == "install":
            install(args.conda, args.bundle, args.prefix, args.source)
            return
        if not Path(sys.prefix).samefile(args.prefix):
            raise ValueError("Run the suite with the Python interpreter from the tested prefix")
        identity = check_runtime(args.conda, args.bundle, args.prefix, args.source)
        loader = unittest.TestLoader()
        tests = list(cases(loader.discover(str(args.source / "tests"))))
        if loader.errors:
            raise ValueError("Test discovery failed: " + "\n".join(str(error) for error in loader.errors))
        if args.command == "verify":
            if args.records is None:
                parser.error("verify requires --records")
            records = [json.loads(path.read_text()) for path in sorted(args.records.glob("shard-*.json"))]
            verify_records(tests, records, identity, args.count)
            print(f"All {len(tests)} discovered tests covered by {args.count} passing shards")
            return
        if args.output is None:
            parser.error("run requires --output")
        selected = partition(tests, args.index, args.count)
        if not selected:
            raise ValueError("CI shard selected no tests")
        identifiers = [test.id() for test in selected]
        result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(unittest.TestSuite(selected))
        if not isinstance(result, TimedResult):
            raise ValueError("CI runner did not produce timing evidence")
        check_runtime(args.conda, args.bundle, args.prefix, args.source)
        record = {"status": "passed" if result.wasSuccessful() and not result.skipped else "failed", "runtime": identity,
                  "index": args.index, "count": args.count, "tests": identifiers, "run": result.testsRun,
                  "skipped": len(result.skipped), "timings": result.timings}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, indent=2) + "\n")
        if record["status"] != "passed":
            parser.exit(1, "CI shard failed or skipped required coverage\n")
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"CI suite rejected: {error}\n")


if __name__ == "__main__":
    main()
