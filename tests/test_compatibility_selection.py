"""Pinned compatibility selection through the maintainer CLI."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests/select_compatibility.py"


class CompatibilitySelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.descriptor = self.root / "baseline.json"
        self.value = {"schema_version": 2, "stable": None,
                      "source": {"repository": "https://github.com/MOMA-AUH/gwflow.git", "commit": "a" * 40}}
        self.output = self.root / "plan.json"

    def command(self, command="plan", *arguments, success=True):
        self.descriptor.write_text(json.dumps(self.value))
        result = subprocess.run([sys.executable, str(TOOL), command, "--descriptor", str(self.descriptor),
                                 *arguments], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def plan(self, success=True):
        return self.command("plan", "--output", str(self.output), success=success)

    def release(self, version):
        capture = self.root / (version + "-capture")
        capture.mkdir()
        filename = f"gwflow-{version[1:]}-py_0.conda"
        package = b"controlled tested gwflow artifact " + version.encode()
        manifest = ("# platform: linux-64\n@EXPLICIT\n"
                    "https://packages.example/linux-64/python-3.12.16-h123_0.conda#" + "1" * 64 + "\n")
        record = {
            "schema_version": 1, "platform": "linux-64", "source_commit": "b" * 40,
            "artifact": {"filename": filename, "sha256": hashlib.sha256(package).hexdigest(), "subdir": "noarch"},
            "manifest": {"filename": "runtime.explicit.txt", "sha256": hashlib.sha256(manifest.encode()).hexdigest()},
            "fixtures": {}, "fixture_versions": {},
        }
        (capture / filename).write_bytes(package)
        (capture / "runtime.explicit.txt").write_text(manifest)
        (capture / "runtime.json").write_text(json.dumps(record))
        qualification = capture / "qualification.json"
        qualification.write_text(json.dumps({
            **record, "status": "qualified", "fresh_cache": True, "fixture_runtime_unchanged": True,
            "smoke": {"command": "python tests/release_smoke.py", "exit_status": 0},
            "packages": {
                "python": {"name": "python", "version": "3.12.16", "build": "h123_0", "subdir": "linux-64",
                           "fn": "python-3.12.16-h123_0.conda", "sha256": "1" * 64},
                "gwflow": {"name": "gwflow", "version": version[1:], "build": "py_0", "subdir": "noarch",
                           "fn": filename, "sha256": record["artifact"]["sha256"]},
            },
        }))
        bundle = self.root / version
        result = subprocess.run([sys.executable, str(ROOT / "tests/release_artifacts.py"), "prepare",
                                 "--capture", str(capture), "--qualification", str(qualification),
                                 "--tag", version, "--source", "b" * 40, "--output", str(bundle)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (bundle / "publication.json").write_text(json.dumps({
            "release": version, "source_commit": "b" * 40,
            "artifact": {**record["artifact"], "url": "https://conda.anaconda.org/moma-auh/noarch/" + filename},
        }))
        return {"version": version, "source_commit": "b" * 40,
                "assets": {path.name: {"url": path.as_uri(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                           for path in bundle.iterdir()}}

    def test_original_latest_and_distinct_intermediate_use_only_checkout_pins(self):
        original, latest = self.release("v1.0.0"), self.release("v1.2.0")
        self.value["stable"] = {"original": original, "latest": latest, "intermediate": []}
        old = json.loads(json.dumps(self.value))
        self.plan()
        plan = json.loads(self.output.read_text())
        self.assertEqual(plan["mode"], "preserved-release")
        self.assertEqual([(entry["version"], entry["roles"]) for entry in plan["baselines"]],
                         [("v1.0.0", ["original"]), ("v1.2.0", ["latest"])])
        intermediate = self.release("v1.1.0")
        intermediate["coverage"] = "Completion records introduced after the original baseline"
        self.value["stable"]["intermediate"] = [intermediate]
        self.plan()
        self.assertEqual([entry["version"] for entry in json.loads(self.output.read_text())["baselines"]],
                         ["v1.0.0", "v1.2.0", "v1.1.0"])
        self.value = old
        self.plan()
        self.assertEqual(json.loads(self.output.read_text()), plan)
        self.value["stable"]["latest"] = original
        self.plan()
        entries = json.loads(self.output.read_text())["baselines"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["roles"], ["original", "latest"])

    def test_download_verifies_preserved_identity_and_reports_each_selected_release(self):
        original, latest = self.release("v1.0.0"), self.release("v1.2.0")
        self.value["stable"] = {"original": original, "latest": latest}
        destination = self.root / "fetched"
        self.command("fetch", "--root", str(destination))
        selection = json.loads((destination / "selection.json").read_text())
        self.assertEqual(selection["status"], "verified")
        self.assertEqual([entry["status"] for entry in selection["baselines"]], ["verified", "verified"])
        self.assertEqual((destination / "v1.0.0/assets/gwflow-1.0.0-py_0.conda").read_bytes(),
                         b"controlled tested gwflow artifact v1.0.0")
        self.assertNotIn(str(self.root), (destination / "selection.json").read_text())
        # Hash failure in one baseline must not hide the independently selected latest baseline.
        (self.root / "v1.0.0/runtime.explicit.txt").write_text("corrupt dependency selection")
        destination = self.root / "corrupt"
        result = self.command("fetch", "--root", str(destination), success=False)
        self.assertIn("v1.0.0", result.stderr)
        selection = json.loads((destination / "selection.json").read_text())
        self.assertEqual(selection["status"], "failed")
        self.assertEqual([entry["status"] for entry in selection["baselines"]], ["failed", "verified"])
        self.assertIn("SHA-256", selection["baselines"][0]["error"])

    def test_unavailable_dependencies_fail_every_selected_comparison_without_substitution(self):
        original, latest = self.release("v1.0.0"), self.release("v1.2.0")
        self.value["stable"] = {"original": original, "latest": latest}
        conda = self.root / "conda"
        conda.write_text("#!/bin/sh\necho 'Exact dependency python-3.12.16-h123_0.conda unavailable' >&2\nexit 1\n")
        conda.chmod(0o755)
        destination = self.root / "matrix"
        result = self.command("run", "--root", str(destination), "--candidate-python", sys.executable,
                              "--candidate-artifact", str(self.root / "v1.2.0/gwflow-1.2.0-py_0.conda"),
                              "--candidate-source", "c" * 40, "--conda", str(conda), success=False)
        self.assertIn("v1.0.0, v1.2.0", result.stderr)
        selection = json.loads((destination / "selection.json").read_text())
        self.assertEqual(selection["status"], "failed")
        for entry in selection["baselines"]:
            self.assertEqual(entry["status"], "failed")
            self.assertIn("Exact dependency", entry["error"])
            comparison = destination / entry["version"] / "comparison"
            self.assertFalse((comparison / "comparison.json").exists())
            selected = json.loads((comparison / "selection.json").read_text())
            self.assertEqual(selected["status"], "unvalidated")
            self.assertTrue(selected["exact_baseline_runtime"])
            self.assertIsNone(selected["replacement_reason"])
            self.assertEqual((comparison / "original-capture/runtime.explicit.txt").read_bytes(),
                             (self.root / entry["version"] / "runtime.explicit.txt").read_bytes())

    def test_corrupt_package_and_manifest_fail_before_any_runtime_creation(self):
        original = self.release("v1.0.0")
        self.value["stable"] = {"original": original, "latest": original}
        for index, filename in enumerate(("gwflow-1.0.0-py_0.conda", "runtime.explicit.txt")):
            path = self.root / "v1.0.0" / filename
            saved = path.read_bytes()
            path.write_bytes(b"untrusted replacement")
            destination = self.root / f"corrupt-{index}"
            self.command("run", "--root", str(destination), "--candidate-python", sys.executable,
                         "--candidate-artifact", str(self.root / "candidate.conda"),
                         "--candidate-source", "c" * 40, "--conda", str(self.root / "must-not-execute"), success=False)
            entry = json.loads((destination / "selection.json").read_text())["baselines"][0]
            self.assertEqual(entry["status"], "failed")
            self.assertIn("SHA-256 mismatch for " + filename, entry["error"])
            self.assertFalse((destination / "v1.0.0/comparison").exists())
            path.write_bytes(saved)

    def test_ineligible_conflicting_or_redundant_selections_are_rejected(self):
        original, latest, intermediate = self.release("v1.0.0"), self.release("v1.2.0"), self.release("v1.1.0")
        invalid = [
            {"original": latest, "latest": original},
            {"original": original, "latest": {**original, "source_commit": "c" * 40}},
            {"original": original, "latest": {**latest, "version": "v0.9.0"}},
            {"original": original, "latest": {**latest, "version": "v1.2.0rc1"}},
            {"original": original, "latest": latest, "intermediate": [intermediate]},
            {"original": original, "latest": latest, "intermediate": [{**original, "coverage": "redundant"}]},
            {"original": original, "latest": latest, "intermediate": [{**intermediate, "coverage": "new state"}] * 2},
            {"original": original, "latest": {**latest, "assets": {}}},
        ]
        for stable in invalid:
            with self.subTest(stable=stable):
                self.value["stable"] = stable
                self.plan(success=False)

    def test_stable_download_rejects_unpublished_package_identity_even_with_matching_pins(self):
        original = self.release("v1.0.0")
        publication = self.root / "v1.0.0/publication.json"
        value = json.loads(publication.read_text())
        value["artifact"]["sha256"] = "0" * 64
        publication.write_text(json.dumps(value))
        original["assets"]["publication.json"]["sha256"] = hashlib.sha256(publication.read_bytes()).hexdigest()
        self.value["stable"] = {"original": original, "latest": original}
        destination = self.root / "unpublished"
        self.command("fetch", "--root", str(destination), success=False)
        entry = json.loads((destination / "selection.json").read_text())["baselines"][0]
        self.assertIn("Published package provenance does not match", entry["error"])

    def test_provisional_fetch_and_run_cannot_report_a_skipped_stable_matrix_as_passing(self):
        destination = self.root / "not-a-matrix"
        result = self.command("fetch", "--root", str(destination), success=False)
        self.assertIn("pinned source regression", result.stderr)
        result = self.command("run", "--root", str(destination), "--candidate-python", sys.executable,
                              "--candidate-artifact", str(self.root / "candidate.conda"),
                              "--candidate-source", "c" * 40, success=False)
        self.assertIn("no stable comparisons performed", result.stderr)
        self.assertFalse(destination.exists())

    def test_pre_stable_checkout_explicitly_retains_pinned_source_comparison(self):
        self.plan()
        plan = json.loads(self.output.read_text())
        self.assertEqual(plan["mode"], "provisional-source")
        self.assertEqual(plan["source"]["commit"], "a" * 40)
        self.assertEqual(plan["baselines"], [])
        self.assertEqual(plan["status"], "selected")
        self.value["source"]["commit"] = "master"
        self.plan(success=False)


if __name__ == "__main__":
    unittest.main()
