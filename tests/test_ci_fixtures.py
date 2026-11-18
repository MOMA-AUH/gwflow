"""Shared container fixture bundles fail closed at their command boundary."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests/ci_fixtures.py"


class CiFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-ci-fixtures-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("container.def", "gwflow-image-tool", "packaged_tasks", "image-build-requirements.txt"):
            source = ROOT / "tests/fixtures" / name
            destination = self.source / "tests/fixtures" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            else:
                shutil.copyfile(source, destination)
        for name in ("tests/ci_fixtures.py", ".github/actions/install-apptainer/action.yml"):
            destination = self.source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixtures")):
            subprocess.run(["git", *args], cwd=self.source, check=True, capture_output=True)
        self.apptainer = self.root / "apptainer"
        self.apptainer.write_text(
            "#!" + sys.executable + "\n"
            "import os, pathlib, sys\n"
            "if sys.argv[1] == '--version':\n"
            "    print('apptainer version 1.5.4')\n"
            "else:\n"
            "    if os.environ.get('FIXTURE_BUILD_FAIL'): sys.exit(23)\n"
            "    pathlib.Path(sys.argv[2]).write_bytes(b'fixture image bytes')\n")
        self.apptainer.chmod(0o755)
        self.bundle = self.root / "bundle"
        self.env = os.environ.copy()
        self.github_env = self.root / "github-env"
        self.cache = self.root / "cache"

    def command(self, command, *, success=True, run_id="fixture-run"):
        result = subprocess.run(
            [sys.executable, str(TOOL), command, "--source", str(self.source), "--bundle", str(self.bundle),
             "--apptainer", str(self.apptainer), "--run-id", run_id,
             *(["--cache", str(self.cache)] if command == "prepare" else []),
             *(["--github-env", str(self.github_env)] if command == "verify" else [])],
            env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_unchanged_cache_is_rebound_to_each_run_without_rebuilding_images(self):
        self.command("prepare")
        first = json.loads((self.bundle / "fixtures.json").read_text())
        self.assertEqual(first["cache_status"], "miss")
        shutil.rmtree(self.bundle)
        self.env["FIXTURE_BUILD_FAIL"] = "1"
        self.command("prepare", run_id="next-run")
        reused = json.loads((self.bundle / "fixtures.json").read_text())
        self.assertEqual(reused["cache_status"], "hit")
        self.assertEqual(reused["run_id"], "next-run")
        self.command("verify", run_id="next-run")

    def test_cache_key_changes_for_every_copied_input_and_build_policy(self):
        initial = self.command("key").stdout.strip()
        self.assertEqual(initial, self.command("key", run_id="unrelated-run").stdout.strip())
        for name in ("tests/fixtures/gwflow-image-tool", "tests/fixtures/container.def",
                     "tests/fixtures/packaged_tasks/packages/summary-task/pyproject.toml",
                     "tests/fixtures/packaged_tasks/packages/report-task/src/report_task/__main__.py",
                     "tests/fixtures/image-build-requirements.txt",
                     "tests/ci_fixtures.py", ".github/actions/install-apptainer/action.yml"):
            with self.subTest(input=name):
                path = self.source / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n# changed input\n")
                self.assertNotEqual(self.command("key").stdout.strip(), initial)
                path.write_bytes(original)
        added = self.source / "tests/fixtures/packaged_tasks/packages/summary-task/extra.txt"
        added.write_text("new copied content")
        self.assertNotEqual(self.command("key").stdout.strip(), initial)

    def test_added_copy_sources_and_runner_image_are_part_of_the_cache_identity(self):
        extra = self.source / "new-input.txt"
        extra.write_text("first")
        definition = self.source / "tests/fixtures/container.def"
        definition.write_text(definition.read_text() + "\n%files\n    new-input.txt /new-input.txt\n")
        initial = self.command("key").stdout.strip()
        extra.write_text("changed")
        self.assertNotEqual(self.command("key").stdout.strip(), initial)
        current = self.command("key").stdout.strip()
        self.env["ImageOS"] = "different-runner-image"
        self.assertNotEqual(self.command("key").stdout.strip(), current)

    def test_mutable_base_images_are_rejected_before_building_or_reusing_cache(self):
        path = self.source / "tests/fixtures/container.def"
        text = path.read_text()
        path.write_text(text.replace(text.splitlines()[1], "From: python:3.12-slim-bookworm"))
        self.command("key", success=False)
        self.command("prepare", success=False)
        self.assertFalse((self.bundle / "fixtures.json").exists())

    def test_invalid_cache_entries_are_rebuilt_and_build_failure_cannot_publish_a_bundle(self):
        self.command("prepare")
        for name in ("basic.sif", "summary.sif", "report.sif", "fixtures.json"):
            for mutation in ("missing", "corrupt"):
                with self.subTest(image=name, mutation=mutation):
                    shutil.rmtree(self.bundle)
                    path = self.cache / name
                    if mutation == "missing":
                        path.unlink()
                    else:
                        path.write_bytes(b"broken cache")
                    self.command("prepare")
                    self.assertEqual(json.loads((self.bundle / "fixtures.json").read_text())["cache_status"], "miss")
                    self.command("verify")
        shutil.rmtree(self.bundle)
        (self.cache / "basic.sif").unlink()
        self.env["FIXTURE_BUILD_FAIL"] = "1"
        self.command("prepare", success=False)
        self.assertFalse((self.bundle / "fixtures.json").exists())

    def test_changed_input_cache_is_rebuilt_before_the_next_run(self):
        self.command("prepare")
        shutil.rmtree(self.bundle)
        path = self.source / "tests/fixtures/gwflow-image-tool"
        path.write_bytes(path.read_bytes() + b"\n# new tool\n")
        self.command("prepare", run_id="changed-run")
        self.assertEqual(json.loads((self.bundle / "fixtures.json").read_text())["cache_status"], "miss")
        self.command("verify", run_id="changed-run")

    def test_shared_images_are_verified_before_enabling_real_container_coverage(self):
        self.command("build")
        manifest = json.loads((self.bundle / "fixtures.json").read_text())
        self.assertEqual(set(manifest["images"]), {"basic.sif", "summary.sif", "report.sif"})
        self.assertEqual(manifest["run_id"], "fixture-run")
        self.assertIn("tests/fixtures/gwflow-image-tool", manifest["inputs"])
        self.command("verify")
        exported = self.github_env.read_text()
        self.assertIn("GWFLOW_TEST_BIOINFORMATICS=1\n", exported)
        for variable, name in (("GWFLOW_TEST_SIF", "basic.sif"), ("GWFLOW_TEST_SUMMARY_SIF", "summary.sif"),
                               ("GWFLOW_TEST_REPORT_SIF", "report.sif")):
            self.assertIn(f"{variable}={self.bundle.resolve() / name}\n", exported)

    def test_failed_preparation_cannot_enable_dependent_container_tests(self):
        self.env["FIXTURE_BUILD_FAIL"] = "1"
        self.command("build", success=False)
        self.assertFalse((self.bundle / "fixtures.json").exists())
        self.command("verify", success=False)
        self.assertFalse(self.github_env.exists())

    def test_missing_or_corrupt_shared_images_fail_without_exporting_test_environment(self):
        self.command("build")
        for name in ("basic.sif", "summary.sif", "report.sif", "fixtures.json"):
            path = self.bundle / name
            original = path.read_bytes()
            for mutation in ("missing", "corrupt"):
                with self.subTest(image=name, mutation=mutation):
                    if mutation == "missing":
                        path.unlink()
                    else:
                        path.write_bytes(b"corrupt artifact")
                    self.command("verify", success=False)
                    self.assertFalse(self.github_env.exists())
                    path.write_bytes(original)

    def test_other_run_or_changed_fixture_sources_cannot_reuse_the_bundle(self):
        self.command("build")
        self.command("verify", run_id="another-run", success=False)
        for name in ("tests/fixtures/gwflow-image-tool", "tests/fixtures/packaged_tasks/packages/summary-task/pyproject.toml",
                     "tests/fixtures/packaged_tasks/packages/report-task/src/report_task/__main__.py",
                     "tests/fixtures/container.def"):
            path = self.source / name
            original = path.read_bytes()
            with self.subTest(source=name):
                path.write_bytes(original + b"\nchanged fixture source\n")
                self.command("verify", success=False)
                self.assertFalse(self.github_env.exists())
                path.write_bytes(original)

    def test_consumers_require_the_pinned_apptainer_runtime(self):
        self.command("build")
        original = self.apptainer.read_text()
        self.apptainer.write_text(original.replace("version 1.5.4", "version 1.5.3"))
        self.command("verify", success=False)
        self.assertFalse(self.github_env.exists())
        self.apptainer.unlink()
        self.command("verify", success=False)
        self.assertFalse(self.github_env.exists())
