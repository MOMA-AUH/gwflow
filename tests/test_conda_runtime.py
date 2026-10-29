"""Capture and restore installed Conda artifacts through maintainer tooling."""

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
TOOL = ROOT / "tests/conda_runtime.py"


class CondaRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prefix = self.root / "tested"
        (self.prefix / "conda-meta").mkdir(parents=True)
        self.artifact = self.root / "gwflow-0.9.0-py_0.conda"
        self.artifact.write_bytes(b"exact tested gwflow artifact")
        self.package("gwflow", "0.9.0", "py_0", "noarch", self.artifact.as_uri(),
                     hashlib.sha256(self.artifact.read_bytes()).hexdigest())
        self.package("python", "3.12.14", "h123_0", "linux-64",
                     "https://packages.example/linux-64/python-3.12.14-h123_0.conda", "1" * 64)
        self.package("libnative", "2.0", "h456_3", "linux-64",
                     "https://packages.example/linux-64/libnative-2.0-h456_3.conda", "2" * 64)
        self.conda = self.root / "conda"
        shutil.copy(ROOT / "tests/fixtures/conda_runtime.py", self.conda)
        self.conda.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        self.bundle = self.root / "preserved"

    def package(self, name, version, build, subdir, url, sha256):
        record = {"name": name, "version": version, "build": build, "subdir": subdir,
                  "url": url, "fn": url.rsplit("/", 1)[1], "sha256": sha256}
        (self.prefix / "conda-meta" / (name + ".json")).write_text(json.dumps(record))

    def command(self, *arguments, success=True):
        result = subprocess.run([sys.executable, str(TOOL), "--conda", str(self.conda), *arguments],
                                text=True, capture_output=True, env=self.environment, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def capture(self, *, success=True):
        return self.command("capture", "--prefix", str(self.prefix), "--artifact", str(self.artifact),
                            "--source", str(ROOT), "--output", str(self.bundle), success=success)

    def test_capture_preserves_complete_runtime_and_separate_exact_package(self):
        self.capture()
        manifest = (self.bundle / "runtime.explicit.txt").read_text()
        self.assertIn("# platform: linux-64\n@EXPLICIT\n", manifest)
        self.assertIn("https://packages.example/linux-64/python-3.12.14-h123_0.conda#" + "1" * 64, manifest)
        self.assertIn("https://packages.example/linux-64/libnative-2.0-h456_3.conda#" + "2" * 64, manifest)
        self.assertNotIn("gwflow-", manifest)
        self.assertNotIn("file:", manifest)
        self.assertEqual((self.bundle / self.artifact.name).read_bytes(), self.artifact.read_bytes())
        record = json.loads((self.bundle / "runtime.json").read_text())
        self.assertEqual(record["platform"], "linux-64")
        self.assertEqual(record["artifact"]["sha256"], hashlib.sha256(self.artifact.read_bytes()).hexdigest())
        self.assertEqual(record["manifest"]["sha256"], hashlib.sha256(manifest.encode()).hexdigest())
        self.assertIn("examples/packaged/packages/summary-task/pyproject.toml", record["fixtures"])

    def test_capture_rejects_unmatched_example_fixtures(self):
        (self.prefix / "extra-packages.json").write_text(json.dumps([
            {"name": "gwflow-summary-task", "version": "0.1.0", "build_string": "pypi_0", "channel": "pypi"}]))
        result = self.capture(success=False)
        self.assertIn("fixture", result.stderr)
        self.assertFalse(self.bundle.exists())

    def test_restore_rejects_changed_package_before_installing(self):
        self.capture()
        (self.bundle / self.artifact.name).write_bytes(b"different package")
        target = self.root / "restored"
        result = self.command("restore", "--capture", str(self.bundle), "--prefix", str(target), success=False)
        self.assertIn("artifact SHA-256", result.stderr)
        self.assertFalse(target.exists())

    def test_restore_uses_exact_artifacts_with_new_environment_and_cache(self):
        self.capture()
        target = self.root / "restored"
        cache = self.root / "fresh-cache"
        self.command("restore", "--capture", str(self.bundle), "--prefix", str(target), "--fresh-cache", str(cache))
        evidence = json.loads((target / "gwflow-runtime.json").read_text())
        self.assertEqual(evidence["status"], "restored")
        self.assertEqual(set(evidence["packages"]), {"gwflow", "python", "libnative"})
        self.assertEqual(evidence["packages"]["python"]["version"], "3.12.14")
        self.assertEqual(evidence["packages"]["libnative"]["build"], "h456_3")
        installation = json.loads((target / "installation.json").read_text())
        self.assertEqual(installation["cache"], str(cache))
        self.assertIn((self.bundle / self.artifact.name).as_uri() + "#" + evidence["artifact"]["sha256"], installation["explicit"])
        result = self.command("restore", "--capture", str(self.bundle), "--prefix", str(target), success=False)
        self.assertIn("new environment", result.stderr)

    def test_capture_rejects_incomplete_export_and_unsupported_runtime(self):
        (self.prefix / "omit-export").touch()
        self.assertIn("complete installed runtime", self.capture(success=False).stderr)
        (self.prefix / "omit-export").unlink()
        (self.prefix / "extra-packages.json").write_text(json.dumps([
            {"name": "runtime-from-pip", "version": "1.0", "build_string": "pypi_0", "channel": "pypi"}]))
        self.assertIn("unsupported package sources", self.capture(success=False).stderr)
        self.assertFalse(self.bundle.exists())

    def test_capture_rejects_missing_hash_url_and_wrong_platform(self):
        path = self.prefix / "conda-meta/libnative.json"
        original = json.loads(path.read_text())
        for field, value, message in (("sha256", "", "SHA-256"), ("url", "", "URL"),
                                      ("url", "file:///temporary/libnative-2.0-h456_3.conda", "URL"),
                                      ("subdir", "osx-arm64", "platform")):
            with self.subTest(field=field, value=value):
                path.write_text(json.dumps({**original, field: value}))
                self.assertIn(message, self.capture(success=False).stderr)
                self.assertFalse(self.bundle.exists())
        path.write_text(json.dumps(original))

    def test_restore_rejects_manifest_changes_platform_mismatch_and_substitution(self):
        self.capture()
        manifest = self.bundle / "runtime.explicit.txt"
        original = manifest.read_bytes()
        manifest.write_bytes(original + b"# changed\n")
        target = self.root / "restored"
        arguments = ("restore", "--capture", str(self.bundle), "--prefix", str(target))
        self.assertIn("manifest SHA-256", self.command(*arguments, success=False).stderr)
        manifest.write_bytes(original)
        record_path = self.bundle / "runtime.json"
        record = json.loads(record_path.read_text())
        record_path.write_text(json.dumps({**record, "platform": "osx-arm64"}))
        self.assertIn("platform", self.command(*arguments, success=False).stderr)
        self.assertFalse(target.exists())
        record_path.write_text(json.dumps(record))
        self.environment["GWFLOW_CONDA_SUBSTITUTE"] = "libnative"
        self.assertIn("differs", self.command(*arguments, success=False).stderr)
        self.assertFalse((target / "gwflow-runtime.json").exists())

    def test_recovered_dependency_requires_identical_recorded_bytes(self):
        recovered = self.root / "libnative-2.0-h456_3.conda"
        recovered.write_bytes(b"original dependency artifact")
        path = self.prefix / "conda-meta/libnative.json"
        package = json.loads(path.read_text())
        package["sha256"] = hashlib.sha256(recovered.read_bytes()).hexdigest()
        path.write_text(json.dumps(package))
        self.capture()
        original = (self.bundle / "runtime.explicit.txt").read_bytes()
        target = self.root / "restored"
        arguments = ("restore", "--capture", str(self.bundle), "--prefix", str(target), "--recovered", str(recovered))
        recovered.write_bytes(b"different dependency artifact")
        self.assertIn("recovered dependency SHA-256", self.command(*arguments, success=False).stderr)
        self.assertFalse(target.exists())
        recovered.write_bytes(b"original dependency artifact")
        self.command(*arguments)
        self.assertEqual((self.bundle / "runtime.explicit.txt").read_bytes(), original)
        installed = json.loads((target / "installation.json").read_text())
        self.assertIn(recovered.as_uri() + "#" + package["sha256"], installed["explicit"])

    def test_qualification_rejects_unmatched_fixture_source_before_installing(self):
        self.capture()
        source = self.root / "changed-source"
        record = json.loads((self.bundle / "runtime.json").read_text())
        for name in record["fixtures"]:
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / name, destination)
        (source / "examples/packaged/packages/summary-task/pyproject.toml").write_text("changed source")
        target = self.root / "restored"
        result = self.command("qualify", "--capture", str(self.bundle), "--prefix", str(target),
                              "--fresh-cache", str(self.root / "cache"), "--source", str(source), success=False)
        self.assertIn("fixture source", result.stderr)
        self.assertFalse(target.exists())
