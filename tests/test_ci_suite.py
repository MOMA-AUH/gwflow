"""CI partitions cannot lose tests, change runtimes, or accept partial success."""

from collections import Counter
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ci_suite import cases, check_runtime, partition, verify_records
import test_conda_runtime


class CiSuiteTests(unittest.TestCase):
    def fixture(self):
        class Case(unittest.TestCase):
            def __init__(self, index):
                super().__init__("runTest")
                self.index = index

            def id(self):
                return f"fixture.Case.test_{self.index}"

        tests = [Case(index) for index in range(80)]
        identity = {"source_commit": "a" * 40, "manifest": {"sha256": "b" * 64}}
        records = []
        for index in range(4):
            identifiers = [test.id() for test in partition(tests, index, 4)]
            records.append({"status": "passed", "index": index, "count": 4, "runtime": identity,
                            "tests": identifiers, "run": len(identifiers), "skipped": 0})
        return tests, identity, records

    def test_partition_preserves_nested_suite_and_each_discovered_case(self):
        tests, identity, records = self.fixture()
        nested = unittest.TestSuite([unittest.TestSuite(tests[:30]), unittest.TestSuite(tests[30:])])
        self.assertEqual(list(cases(nested)), tests)
        self.assertEqual(Counter(test.id() for test in tests), Counter(name for record in records for name in record["tests"]))
        self.assertEqual(partition(tests, 2, 4), list(reversed(partition(list(reversed(tests)), 2, 4))))
        verify_records(tests, records, identity, 4)

    def test_missing_duplicate_failed_skipped_or_changed_shards_are_rejected(self):
        tests, identity, records = self.fixture()
        for mutation in ("missing-shard", "duplicate-shard", "missing-test", "duplicate-test", "wrong-partition",
                         "failure", "runtime", "count", "run", "skip"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(records)
                if mutation == "missing-shard":
                    changed.pop()
                elif mutation == "duplicate-shard":
                    changed[1] = changed[0]
                elif mutation == "missing-test":
                    changed[0]["tests"].pop()
                elif mutation == "duplicate-test":
                    changed[0]["tests"].append(changed[0]["tests"][0])
                elif mutation == "wrong-partition":
                    changed[0]["tests"], changed[1]["tests"] = changed[1]["tests"], changed[0]["tests"]
                elif mutation == "failure":
                    changed[0]["status"] = "failed"
                elif mutation == "runtime":
                    changed[0]["runtime"]["manifest"]["sha256"] = "c" * 64
                elif mutation == "count":
                    changed[0]["count"] = 3
                elif mutation == "run":
                    changed[0]["run"] -= 1
                else:
                    changed[0]["skipped"] = 1
                with self.assertRaises(ValueError):
                    verify_records(tests, changed, identity, 4)

    def test_invalid_partition_is_rejected(self):
        for index, count in ((-1, 4), (4, 4), (0, 0)):
            with self.subTest(index=index, count=count), self.assertRaises(ValueError):
                partition([], index, count)

    def test_runtime_transfer_checks_source_manifest_and_installed_bytes(self):
        fixture = test_conda_runtime.CondaRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture()
        source = Path(__file__).resolve().parents[1]
        identity = check_runtime(str(fixture.conda), fixture.bundle, fixture.prefix, source)
        self.assertEqual(identity["artifact"], json.loads((fixture.bundle / "runtime.json").read_text())["artifact"])
        record_path = fixture.bundle / "runtime.json"
        original = record_path.read_text()
        record = json.loads(original)
        record["source_commit"] = "b" * 40
        record_path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "differs from the shared build"):
            check_runtime(str(fixture.conda), fixture.bundle, fixture.prefix, source)
        record_path.write_text(original)
        (fixture.prefix / "lib/gwflow.payload").write_text("changed installed package")
        with self.assertRaises(ValueError):
            check_runtime(str(fixture.conda), fixture.bundle, fixture.prefix, source)

    def test_runner_records_passes_and_rejects_failures_skips_and_discovery_errors(self):
        # Only Conda inventory is controlled here; execute discovery, tests, timing,
        # result writing, and the coverage gate through the real command boundary.
        wrapper = ("import sys; sys.path.insert(0, sys.argv.pop(1)); import ci_suite; "
                   "ci_suite.check_runtime = lambda *args: {}; ci_suite.main()")
        with tempfile.TemporaryDirectory(prefix="gwflow-ci-runner-") as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            output = root / "records/shard-0.json"
            for outcome, body in (("pass", "self.assertTrue(True)"), ("fail", "self.fail('controlled failure')"),
                                  ("skip", "self.skipTest('controlled skip')"), ("discovery", "invalid Python :")):
                with self.subTest(outcome=outcome):
                    output.unlink(missing_ok=True)
                    (root / "tests/test_probe.py").write_text(
                        "import unittest\nclass Probe(unittest.TestCase):\n    def test_probe(self):\n        " + body + "\n")
                    arguments = [sys.executable, "-B", "-c", wrapper, str(Path(__file__).parent.resolve()),
                                 "run", "--source", str(root), "--bundle", str(root), "--prefix", sys.prefix,
                                 "--count", "1", "--output", str(output)]
                    result = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode == 0, outcome == "pass", result.stdout + result.stderr)
                    if outcome == "discovery":
                        self.assertFalse(output.exists())
                        continue
                    record = json.loads(output.read_text())
                    self.assertEqual(record["run"], 1)
                    self.assertEqual(record["tests"], ["test_probe.Probe.test_probe"])
                    self.assertEqual(record["timings"][0]["test"], record["tests"][0])
                    if outcome == "pass":
                        arguments[arguments.index("run")] = "verify"
                        arguments += ["--records", str(output.parent)]
                        subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=20)
                    else:
                        self.assertEqual(record["status"], "failed")
