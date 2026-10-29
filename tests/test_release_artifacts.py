"""Release preparation through the maintainer artifact command."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests/release_artifacts.py"


class ReleaseArtifactsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.capture = self.root / "capture"
        self.capture.mkdir()
        self.output = self.root / "assets"
        self.source = "a" * 40
        self.version = "1.0.0"
        self.environment = os.environ.copy()
        self.fixture()

    def fixture(self):
        filename = f"gwflow-{self.version}-py_0.conda"
        package = b"exact tested and qualified gwflow bytes"
        manifest = ("# platform: linux-64\n@EXPLICIT\n"
                    "https://packages.example/linux-64/python-3.12.16-h123_0.conda#" + "1" * 64 + "\n")
        self.record = {
            "schema_version": 1, "platform": "linux-64", "source_commit": self.source,
            "artifact": {"filename": filename, "sha256": hashlib.sha256(package).hexdigest(), "subdir": "noarch"},
            "manifest": {"filename": "runtime.explicit.txt", "sha256": hashlib.sha256(manifest.encode()).hexdigest()},
            "fixtures": {"examples/packaged/packages/fixture.py": "2" * 64},
            "fixture_versions": {"gwflow-summary-task": "0.4.0", "gwflow-report-task": "0.4.0"},
        }
        (self.capture / filename).write_bytes(package)
        (self.capture / "runtime.explicit.txt").write_text(manifest)
        (self.capture / "runtime.json").write_text(json.dumps(self.record))
        self.qualification = self.root / "qualification.json"
        self.evidence = {
            "status": "qualified", "platform": "linux-64", "fresh_cache": True,
            "fixture_runtime_unchanged": True, "source_commit": self.source,
            "fixture_versions": self.record["fixture_versions"],
            "artifact": self.record["artifact"], "manifest": self.record["manifest"],
            "smoke": {"command": "python tests/release_smoke.py", "exit_status": 0},
            "packages": {
                "python": {"name": "python", "version": "3.12.16", "build": "h123_0",
                           "subdir": "linux-64", "fn": "python-3.12.16-h123_0.conda", "sha256": "1" * 64},
                "gwflow": {"name": "gwflow", "version": self.version, "build": "py_0",
                           "subdir": "noarch", "fn": filename, "sha256": self.record["artifact"]["sha256"]},
            },
        }
        self.qualification.write_text(json.dumps(self.evidence))

    def command(self, *arguments, success=True):
        result = subprocess.run([sys.executable, str(TOOL), *arguments], capture_output=True, text=True,
                                env=self.environment, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare(self, success=True):
        return self.command("prepare", "--capture", str(self.capture), "--qualification", str(self.qualification),
                            "--tag", "v" + self.version, "--source", self.source,
                            "--output", str(self.output), success=success)

    def test_preparation_preserves_exact_qualified_package_and_manifest(self):
        self.prepare()
        self.assertEqual((self.output / self.record["artifact"]["filename"]).read_bytes(),
                         b"exact tested and qualified gwflow bytes")
        self.assertEqual((self.output / "runtime.explicit.txt").read_bytes(),
                         (self.capture / "runtime.explicit.txt").read_bytes())
        release = json.loads((self.output / "release.json").read_text())
        self.assertEqual(release["release"], "v1.0.0")
        self.assertTrue(release["stable_baseline_eligible"])
        self.assertEqual(release["source_commit"], self.source)
        checksums = dict(line.split("  ", 1)[::-1] for line in (self.output / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(checksums[self.record["artifact"]["filename"]], self.record["artifact"]["sha256"])
        self.assertEqual(checksums["runtime.explicit.txt"], self.record["manifest"]["sha256"])
        self.command("verify", "--bundle", str(self.output), "--tag", "v1.0.0", "--source", self.source)

    def test_failed_or_unmatched_qualification_never_prepares_publication(self):
        changes = (
            {"status": "restored"},
            {"fresh_cache": False},
            {"fixture_runtime_unchanged": False},
            {"smoke": {"command": "python tests/release_smoke.py", "exit_status": 1}},
            {"source_commit": "b" * 40},
            {"artifact": {**self.record["artifact"], "sha256": "0" * 64}},
            {"manifest": {**self.record["manifest"], "sha256": "0" * 64}},
            {"fixture_versions": {"gwflow-summary-task": "different"}},
            {"packages": {"gwflow": self.evidence["packages"]["gwflow"]}},
        )
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                self.output = self.root / f"rejected-{index}"
                self.qualification.write_text(json.dumps({**self.evidence, **change}))
                self.prepare(success=False)
                self.assertFalse(self.output.exists())

    def test_transferred_bundle_rejects_changed_or_missing_release_identities(self):
        self.prepare()
        checksums = self.output / "SHA256SUMS"
        original = checksums.read_text()
        checksums.write_text("")
        arguments = ("verify", "--bundle", str(self.output), "--tag", "v1.0.0", "--source", self.source)
        self.command(*arguments, success=False)
        checksums.write_text(original)
        manifest = self.output / "runtime.explicit.txt"
        manifest.write_text(manifest.read_text() + "# unexpected change\n")
        self.command(*arguments, success=False)

    def test_only_final_releases_from_one_zero_are_stable_baselines(self):
        for version, stable in (("0.9.0", False), ("1.0.0rc1", False), ("1.0.1.dev1", False),
                                ("1.0.1", True), ("2.0.0", True)):
            with self.subTest(version=version):
                self.version = version
                self.fixture()
                self.output = self.root / ("assets-" + version)
                self.prepare()
                record = json.loads((self.output / "release.json").read_text())
                self.assertEqual(record["stable_baseline_eligible"], stable)

    def publication(self):
        self.provenance = self.output / "publication.json"
        self.published = {"release": "v" + self.version, "source_commit": self.source,
                          "artifact": {**self.record["artifact"],
                                       "url": "https://conda.anaconda.org/moma-auh/noarch/" + self.record["artifact"]["filename"]}}
        self.provenance.write_text(json.dumps(self.published))
        self.github = self.root / "gh"
        shutil.copy(ROOT / "tests/fixtures/release_github.py", self.github)
        self.github.chmod(0o755)
        self.remote = self.root / "remote"
        self.environment["GWFLOW_RELEASE_FIXTURE_ROOT"] = str(self.remote)

    def attach(self, success=True):
        return self.command("attach", "--bundle", str(self.output), "--publication", str(self.provenance),
                            "--tag", "v" + self.version, "--source", self.source,
                            "--repo", "MOMA-AUH/gwflow", "--gh", str(self.github), success=success)

    def test_stable_assets_are_preserved_in_a_draft_after_published_identity_matches(self):
        self.prepare()
        self.publication()
        self.attach()
        state = json.loads((self.remote / "state.json").read_text())
        self.assertEqual(state, {"tagName": "v1.0.0", "isDraft": True, "isPrerelease": False})
        for name in (self.record["artifact"]["filename"], "runtime.explicit.txt", "runtime.json",
                     "qualification.json", "release.json", "SHA256SUMS", "publication.json"):
            self.assertEqual((self.remote / "assets" / name).read_bytes(), (self.output / name).read_bytes())

    def test_wrong_publication_and_nonfinal_versions_cannot_attach_stable_assets(self):
        self.prepare()
        self.publication()
        self.published["artifact"]["sha256"] = "0" * 64
        self.provenance.write_text(json.dumps(self.published))
        self.attach(success=False)
        self.assertFalse(self.remote.exists())
        self.published["artifact"]["sha256"] = self.record["artifact"]["sha256"]
        self.provenance.write_text(json.dumps(self.published))
        (self.output / "qualification.json").write_text("{}")
        self.attach(success=False)
        self.assertFalse(self.remote.exists())
        for version in ("0.9.0", "1.0.0rc1", "1.1.0.dev1"):
            with self.subTest(version=version):
                self.version = version
                self.fixture()
                self.output = self.root / ("assets-" + version)
                self.prepare()
                self.publication()
                self.attach()
                self.assertFalse(self.remote.exists())

    def test_draft_resumes_identical_assets_but_never_replaces_existing_bytes(self):
        self.prepare()
        self.publication()
        self.attach()
        calls = self.remote / "calls.jsonl"
        calls.write_text("")
        self.attach()
        self.assertNotIn('"upload"', calls.read_text())
        package = self.remote / "assets" / self.record["artifact"]["filename"]
        package.write_bytes(b"different existing release bytes")
        calls.write_text("")
        result = self.attach(success=False)
        self.assertIn("refusing replacement", result.stderr)
        self.assertNotIn('"upload"', calls.read_text())
        self.assertEqual(package.read_bytes(), b"different existing release bytes")

    def test_existing_prerelease_or_published_release_cannot_be_mutated_as_a_stable_draft(self):
        self.prepare()
        self.publication()
        self.remote.mkdir()
        (self.remote / "assets").mkdir()
        for draft, prerelease in ((True, True), (False, False), (False, True)):
            with self.subTest(draft=draft, prerelease=prerelease):
                state = {"tagName": "v1.0.0", "isDraft": draft, "isPrerelease": prerelease}
                (self.remote / "state.json").write_text(json.dumps(state))
                calls = self.remote / "calls.jsonl"
                calls.write_text("")
                self.attach(success=False)
                self.assertNotIn('"upload"', calls.read_text())
                self.assertNotIn('"create"', calls.read_text())
                self.assertEqual(json.loads((self.remote / "state.json").read_text()), state)


if __name__ == "__main__":
    unittest.main()
