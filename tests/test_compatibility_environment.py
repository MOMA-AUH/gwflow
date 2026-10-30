"""Independent runtime validation through the installed-environment command."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests/compatibility_environment.py"


class CompatibilityEnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-independent-")
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        wheels = cls.root / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--wheel-dir", str(wheels), str(ROOT)], check=True, capture_output=True, text=True)
        cls.prefix = cls.root / "shared"
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(cls.prefix)],
                       check=True, capture_output=True, text=True)
        cls.python = cls.prefix / "bin/python"
        subprocess.run([str(cls.python), "-m", "pip", "install", "--no-deps", "--ignore-installed", "--no-index",
                        str(next(wheels.glob("gwflow-*.whl")))], check=True, capture_output=True, text=True)

    def command(self, *arguments):
        return subprocess.run([sys.executable, str(TOOL), "--python", str(self.python), "--source", "fixture",
                               *arguments], capture_output=True, text=True, timeout=30)

    def test_independent_check_rejects_shared_dependencies_despite_owned_gwflow(self):
        result = self.command("--independent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside its own environment", result.stderr)

    def test_provisional_check_records_shared_runtime_without_claiming_independence(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["dependency_policy"], "provisional-shared-runtime")
        self.assertEqual(record["versions"]["gwflow"], tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"])
        self.assertEqual(record["supported_runtime"], True)

    def test_each_installation_must_satisfy_its_own_package_requirements(self):
        metadata = next(self.prefix.glob("lib/python*/site-packages/gwflow-*.dist-info/METADATA"))
        original = metadata.read_text()
        python_requirement = next(line for line in original.splitlines() if line.startswith("Requires-Python:"))
        gwf_requirement = next(line for line in original.splitlines() if line.startswith("Requires-Dist: gwf"))
        for before, after in ((python_requirement, "Requires-Python: >=99"),
                              (gwf_requirement, "Requires-Dist: gwf==99")):
            with self.subTest(requirement=after):
                self.assertIn(before, original)
                try:
                    metadata.write_text(original.replace(before, after))
                    result = self.command()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("does not satisfy", result.stderr)
                finally:
                    metadata.write_text(original)


if __name__ == "__main__":
    unittest.main()
