"""Conda download-cache identity and archive boundaries at the command interface."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests/conda_downloads.py"


class CondaDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-download-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        for name in ("environment.yml", "pyproject.toml", "conda-recipe/meta.yaml",
                     ".github/actions/cache-conda-downloads/action.yml", "tests/conda_downloads.py", "tests/prepare_ci_conda.sh"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)

    def command(self, *arguments, success=True):
        result = subprocess.run([sys.executable, str(TOOL), *arguments], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def key(self, profile="source", version="conda 26.9.4", *extra):
        return self.command("key", "--source", str(self.source), "--profile", profile,
                            "--conda-version", version, *extra).stdout.strip()

    def test_identity_tracks_dependencies_tool_platform_and_explicit_runtime(self):
        original = self.key()
        self.assertEqual(original, self.key())
        self.assertNotEqual(original, self.key("build"))
        self.assertNotEqual(original, self.key(version="conda 27.0"))
        self.assertNotEqual(original, self.key("source", "conda 26.9.4", "--platform", "different"))
        (self.source / "environment.yml").write_text("changed dependencies")
        self.assertNotEqual(original, self.key())
        self.command("key", "--source", str(self.source), "--profile", "runtime",
                     "--conda-version", "conda 26.9.4", success=False)
        manifest = self.root / "runtime.explicit.txt"
        manifest.write_text("@EXPLICIT\nhttps://example.test/python.conda#abc\n")
        before = self.key("runtime", "conda 26.9.4", "--manifest", str(manifest))
        manifest.write_text("@EXPLICIT\nhttps://example.test/python.conda#def\n")
        self.assertNotEqual(before, self.key("runtime", "conda 26.9.4", "--manifest", str(manifest)))

    def test_summary_counts_only_dependency_archives_never_installed_state(self):
        packages = self.root / "packages"
        packages.mkdir()
        (packages / "python-3.12-h0.conda").write_bytes(b"abc")
        (packages / "lib-1-h0.tar.bz2").write_bytes(b"12345")
        (packages / "gwflow-0.11.2-py_0.conda").write_bytes(b"candidate")
        (packages / "extracted").mkdir()
        (packages / "extracted/file").write_bytes(b"installed payload")
        (packages / "urls.txt").write_text("metadata")
        output = self.command("summary", "--packages", str(packages)).stdout
        record = json.loads(output)
        self.assertEqual(record["archive_bytes"], 8)
        self.assertEqual(record["archive_count"], 2)
        self.assertEqual(set(record["archives"]), {"python-3.12-h0.conda", "lib-1-h0.tar.bz2"})
        self.assertEqual(json.loads(self.command("summary", "--packages", str(self.root / "absent")).stdout)["archive_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
