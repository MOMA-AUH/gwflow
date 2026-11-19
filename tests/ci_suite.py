"""Run installed-package tests and verify complete, unchanged-runtime coverage."""

import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
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
    # Package builders write metadata beside their inputs. Keep the checkout
    # unchanged so shared container fixture identities still describe it.
    with tempfile.TemporaryDirectory(prefix="gwflow-ci-fixture-install-") as directory:
        projects = []
        for name in ("summary-task", "report-task"):
            staged = Path(directory) / name
            shutil.copytree(source / "tests/fixtures/packaged_tasks/packages" / name, staged)
            projects.append(str(staged))
        run(str(prefix / "bin/python"), "-m", "pip", "install", "--no-deps", *projects, show_output=True)
    check_runtime(conda, bundle, prefix, source)


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


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


def verify_record(tests: list, record: dict, identity: dict) -> None:
    expected = Counter(test.id() for test in tests)
    if (not expected or record["status"] != "passed" or record["runtime"] != identity
            or record["run"] != sum(expected.values()) or Counter(record["tests"]) != expected
            or record["skipped"] != 0):
        raise ValueError("Incomplete, failed, or mismatched CI suite")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "run", "verify"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--conda", default="conda")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    if args.bundle is None or args.prefix is None:
        parser.error(f"{args.command} requires --bundle and --prefix")
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
            if args.record is None:
                parser.error("verify requires --record")
            verify_record(tests, json.loads(args.record.read_text()), identity)
            print(f"All {len(tests)} discovered tests covered by the passing suite")
            return
        if args.output is None:
            parser.error("run requires --output")
        if not tests:
            raise ValueError("CI suite discovered no tests")
        identifiers = [test.id() for test in tests]
        result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(unittest.TestSuite(tests))
        if not isinstance(result, TimedResult):
            raise ValueError("CI runner did not produce timing evidence")
        check_runtime(args.conda, args.bundle, args.prefix, args.source)
        record = {"status": "passed" if result.wasSuccessful() and not result.skipped else "failed", "runtime": identity,
                  "tests": identifiers, "run": result.testsRun,
                  "skipped": len(result.skipped), "timings": result.timings}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, indent=2) + "\n")
        if record["status"] != "passed":
            parser.exit(1, "CI suite failed or skipped required coverage\n")
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"CI suite rejected: {error}\n")


if __name__ == "__main__":
    main()
