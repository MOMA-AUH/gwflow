"""Preserved baseline failures through the independent comparison command."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests/compare_runtime.py"


class RuntimeComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-compare-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.artifact = self.bundle / "gwflow-0.9.0-py_0.conda"
        self.artifact.write_bytes(b"preserved package")
        manifest = "# platform: linux-64\n@EXPLICIT\nhttps://packages.example/linux-64/python-3.12.14-h123_0.conda#" + "1" * 64 + "\n"
        (self.bundle / "runtime.explicit.txt").write_text(manifest)
        self.record = {"schema_version": 1, "platform": "linux-64", "source_commit": "a" * 40,
                       "artifact": {"filename": self.artifact.name, "sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest(), "subdir": "noarch"},
                       "manifest": {"filename": "runtime.explicit.txt", "sha256": hashlib.sha256(manifest.encode()).hexdigest()},
                       "fixtures": {}, "fixture_versions": {"gwflow-summary-task": "0.4.0", "gwflow-report-task": "0.4.0"}}
        (self.bundle / "runtime.json").write_text(json.dumps(self.record))
        self.conda = self.root / "conda"
        shutil.copy(ROOT / "tests/fixtures/conda_runtime.py", self.conda)
        self.conda.chmod(0o755)
        self.output = self.root / "comparison"
        self.environment = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        self.candidate_python = Path(sys.executable)

    def command(self, *arguments):
        return subprocess.run([sys.executable, str(TOOL), "--conda", str(self.conda),
                               "--baseline-capture", str(self.bundle), "--baseline-kind", "controlled-artifact",
                               "--candidate-python", str(self.candidate_python), "--candidate-artifact", str(self.artifact),
                               "--candidate-source", "candidate",
                               "--root", str(self.output), *arguments], capture_output=True, text=True,
                              env=self.environment, timeout=30)

    def inspected_runtimes(self, examples):
        # Control only external Conda/interpreter observations. The real comparison
        # and journey runner validate the installations and the test selection.
        probe = self.root / "runtime-probe"
        shutil.copy(ROOT / "tests/fixtures/runtime_probe.py", probe)
        probe.chmod(0o755)
        self.environment["GWFLOW_CONDA_PROBE"] = str(probe)
        self.environment["GWFLOW_CONDA_EXAMPLES"] = json.dumps([
            {"name": name, "version": version, "channel": "pypi"}
            for name, version in self.record["fixture_versions"].items()])
        prefix = self.root / "candidate"
        explicit = self.root / "candidate.explicit.txt"
        explicit.write_text("@EXPLICIT" + (self.bundle / "runtime.explicit.txt").read_text().split("@EXPLICIT", 1)[1]
                            + self.artifact.as_uri() + "#" + self.record["artifact"]["sha256"] + "\n")
        subprocess.run([str(self.conda), "create", "--prefix", str(prefix), "--no-default-packages",
                        "--file", str(explicit)], env=self.environment, check=True, capture_output=True, text=True)
        (prefix / "extra-packages.json").write_text(json.dumps(examples))
        self.candidate_python = prefix / "bin/python"

    def source_examples(self):
        return [{"name": project["name"], "version": project["version"], "channel": "pypi"}
                for path in sorted((ROOT / "examples/packaged/packages").glob("*/pyproject.toml"))
                for project in [tomllib.loads(path.read_text())["project"]]]

    def test_candidate_examples_use_source_versions_independently_of_preserved_baseline(self):
        self.inspected_runtimes(self.source_examples())
        result = self.command("--test", "no-such-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No compatibility tests matched", result.stderr)
        self.assertTrue((self.output / "journeys/environment.json").exists())
        self.assertFalse((self.output / "comparison.json").exists())

    def test_candidate_without_examples_reaches_the_same_journey_selection(self):
        self.inspected_runtimes([])
        result = self.command("--test", "no-such-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No compatibility tests matched", result.stderr)
        self.assertTrue((self.output / "journeys/environment.json").exists())
        self.assertFalse((self.output / "comparison.json").exists())

    def test_unexpected_candidate_example_versions_fail_before_journeys(self):
        examples = self.source_examples()
        self.inspected_runtimes(examples)
        for index, example in enumerate(examples):
            with self.subTest(package=example["name"]):
                self.output = self.root / f"mismatched-{index}"
                changed = [{**item, "version": "99.0.0"} if item == example else item for item in examples]
                (self.candidate_python.parent.parent / "extra-packages.json").write_text(json.dumps(changed))
                result = self.command("--test", "no-such-test")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("example fixture version", result.stderr)
                self.assertIn(example["name"], result.stderr)
                self.assertIn(f"installed 99.0.0, expected {example['version']}", result.stderr)
                self.assertFalse((self.output / "journeys").exists())
                self.assertFalse((self.output / "comparison.json").exists())

    def test_candidate_examples_do_not_bypass_artifact_identity(self):
        self.inspected_runtimes(self.source_examples())
        wrong_artifact = self.root / "different.conda"
        wrong_artifact.write_bytes(b"different candidate artifact")
        result = self.command("--candidate-artifact", str(wrong_artifact), "--test", "no-such-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Candidate artifact SHA-256", result.stderr)
        self.assertFalse((self.output / "journeys").exists())

    def test_candidate_examples_do_not_bypass_installed_file_integrity(self):
        self.inspected_runtimes(self.source_examples())
        (self.candidate_python.parent.parent / "lib/gwflow.payload").write_text("modified installed file")
        result = self.command("--test", "no-such-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Installed file differs from its Conda artifact", result.stderr)
        self.assertFalse((self.output / "journeys").exists())

    def test_corrupt_preserved_package_fails_before_installation_or_journeys(self):
        self.artifact.write_bytes(b"corrupt package")
        result = self.command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact SHA-256", result.stderr)
        self.assertFalse((self.output / "baseline").exists())
        self.assertFalse((self.output / "journeys/summary.json").exists())

    def test_replacement_cannot_change_the_preserved_gwflow_package(self):
        replacement = self.root / "replacement"
        shutil.copytree(self.bundle, replacement)
        (replacement / self.artifact.name).write_bytes(b"replacement gwflow")
        record = {**self.record, "artifact": {**self.record["artifact"],
                  "sha256": hashlib.sha256(b"replacement gwflow").hexdigest()}}
        (replacement / "runtime.json").write_text(json.dumps(record))
        result = self.command("--replacement-capture", str(replacement), "--replacement-reason", "lost dependency")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Replacement must preserve the original artifact", result.stderr)
        self.assertFalse((self.output / "baseline").exists())

    def test_missing_dependency_fails_comparison_without_fallback_or_success_record(self):
        self.environment["GWFLOW_CONDA_UNAVAILABLE"] = "1"
        result = self.command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dependency artifact unavailable", result.stderr)
        self.assertFalse((self.output / "baseline").exists())
        self.assertFalse((self.output / "comparison.json").exists())
        self.assertFalse((self.output / "journeys").exists())

    def test_explicit_replacement_retains_original_manifest_and_reason_when_validation_fails(self):
        original = (self.bundle / "runtime.explicit.txt").read_bytes()
        replacement = self.root / "replacement"
        shutil.copytree(self.bundle, replacement)
        manifest = original.replace(b"3.12.14", b"3.12.13")
        (replacement / "runtime.explicit.txt").write_bytes(manifest)
        record = {**self.record, "manifest": {**self.record["manifest"], "sha256": hashlib.sha256(manifest).hexdigest()}}
        (replacement / "runtime.json").write_text(json.dumps(record))
        self.environment["GWFLOW_CONDA_UNAVAILABLE"] = "1"
        reason = "Permanent dependency loss; controlled replacement proposal"
        result = self.command("--replacement-capture", str(replacement), "--replacement-reason", reason)
        self.assertNotEqual(result.returncode, 0)
        selection = json.loads((self.output / "selection.json").read_text())
        self.assertEqual(selection["status"], "unvalidated")
        self.assertFalse(selection["exact_baseline_runtime"])
        self.assertEqual(selection["replacement_reason"], reason)
        self.assertEqual((self.output / "original-capture/runtime.explicit.txt").read_bytes(), original)
        self.assertEqual((self.output / "replacement-capture/runtime.explicit.txt").read_bytes(), manifest)
        self.assertEqual((self.output / "replacement-capture" / self.artifact.name).read_bytes(), self.artifact.read_bytes())
        self.assertFalse((self.output / "comparison.json").exists())


if __name__ == "__main__":
    unittest.main()
