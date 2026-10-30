"""Independent runtime validation through the installed-environment command."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from copy import deepcopy


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

    def test_comparison_rejects_nested_installations_before_starting_work(self):
        nested = self.prefix / "nested"
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(nested)],
                       check=True, capture_output=True, text=True)
        subprocess.run([str(nested / "bin/python"), "-m", "pip", "install", "--no-deps", "--ignore-installed", "--no-index",
                        str(next((self.root / "wheels").glob("gwflow-*.whl")))], check=True, capture_output=True, text=True)
        output = self.root / "nested-comparison"
        result = subprocess.run([sys.executable, str(ROOT / "tests/validate_compatibility.py"),
                                 "--baseline-python", str(nested / "bin/python"), "--candidate-python", str(self.python),
                                 "--baseline-source", "fixture", "--candidate-source", "fixture", "--root", str(output),
                                 "--test", "no-such-test"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disjoint", result.stderr)
        self.assertFalse(output.exists())

    def test_independent_comparison_rejects_shared_observed_runtime_directory(self):
        observed = json.loads(self.command().stdout)
        # Model two bind-mount views of the same real site-packages directory.
        # Only the external interpreter observation is controlled; no mount is needed.
        shared = Path(sys.prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        identity = {"device": shared.stat().st_dev, "inode": shared.stat().st_ino}
        interpreters = []
        for side in ("baseline", "candidate"):
            prefix = self.root / ("observed-" + side)
            prefix.mkdir()
            record = deepcopy(observed)
            for key in ("source", "supported_runtime", "dependency_policy"):
                record.pop(key)
            record.update(prefix=str(prefix), base_prefix=str(prefix), executable=str(prefix / "python"),
                          package=str(prefix / "site-packages/gwflow"),
                          stdlib=str(prefix / "stdlib"), sys_path=[str(prefix / "stdlib"), str(prefix / "site-packages")],
                          prefix_identity={"device": prefix.stat().st_dev, "inode": prefix.stat().st_ino},
                          runtime_directories=[{"path": str(prefix / "site-packages"), **identity}])
            for item in record["distributions"]:
                item["location"] = str(prefix / "site-packages")
            record["imports"] = {name: [str(prefix / "site-packages" / name)] for name in record["imports"]}
            executable = prefix / "python"
            executable.write_text(f"#!{sys.executable}\nprint({json.dumps(record)!r})\n")
            executable.chmod(0o755)
            interpreters.append(executable)
        output = self.root / "shared-directory-comparison"
        result = subprocess.run([sys.executable, str(ROOT / "tests/validate_compatibility.py"), "--independent",
                                 "--baseline-python", str(interpreters[0]), "--candidate-python", str(interpreters[1]),
                                 "--baseline-source", "fixture", "--candidate-source", "fixture", "--root", str(output),
                                 "--test", "no-such-test"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("share an installed runtime directory", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
