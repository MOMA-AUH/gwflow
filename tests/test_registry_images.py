"""Registry acquisition through public workflow declarations and installed CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from gwf.exceptions import WorkflowError
from gwflow import Task

from support import FIXTURES, GWF, LocalBackendTestCase
import test_containers
import test_managed
import test_managed_recovery


class RegistryAuthoringTests(unittest.TestCase):
    def test_tag_digest_and_local_path_declarations_do_not_acquire(self):
        for reference in ("docker://ubuntu", "docker://example.org:5000/tools/demo:1.2",
                          "docker://tools/demo@sha256:" + "a" * 64,
                          "docker://tools/demo:1.2@sha256:" + "b" * 64,
                          "docker://[::1]:5000/tools/demo:1.2", Path("local image.sif"), "bare-name"):
            with self.subTest(reference=reference):
                target = Task(inputs=[]).target("compute", inputs=[], outputs=["out"], image=reference)
                self.assertEqual(target.image, str(reference))

    def test_malformed_and_unsupported_references_are_authoring_errors(self):
        for reference in ("docker://", "docker:///image", "docker://foo/", "docker://a b/c",
                          "docker://tools/UPPER", "docker://tools/demo:", "docker://tools/demo@sha256:bad",
                          "docker://user:password@example.org/demo", "https://example.org/demo.sif",
                          "library://tools/demo", "docker:/demo", "docker://demo?tag=one",
                          "docker://tools/demo@sha256:" + "a" * 32,
                          "docker://tools/demo@unknown:" + "a" * 64,
                          "docker://tools/demo@SHA256:" + "a" * 64):
            with self.subTest(reference=reference), self.assertRaises(WorkflowError):
                Task(inputs=[]).target("compute", inputs=[], outputs=["out"], image=reference)


class RegistryTestCase(LocalBackendTestCase):
    snapshot = test_containers.ContainerRuntimeTests.snapshot
    attempt = test_containers.ContainerRuntimeTests.attempt
    settle = test_managed.ManagedCliTests.settle
    inject = test_managed_recovery.ManagedCoordinationTests.inject
    reference = "docker://example.org/tools/demo:latest"

    def configure_workflow(self, reference=None, *, other=False):
        if not hasattr(self, "controller"):
            self.controller = self.work / "image fixture"
            self.controller.mkdir()
            self.cache = self.work / "shared image cache"
            self.bin = self.controller / "bin"
            self.bin.mkdir()
            executable = self.bin / "apptainer"
            executable.write_text(f"#!{sys.executable}\n" + (FIXTURES / "apptainer_fixture.py").read_text())
            executable.chmod(0o755)
            self.options()
            environment = patch.dict(os.environ, {
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "GWFLOW_IMAGE_CACHE": str(self.cache),
                "GWFLOW_TEST_IMAGE_FIXTURE": str(self.controller),
            })
            environment.start()
            self.addCleanup(environment.stop)
        reference = self.reference if reference is None else reference
        declaration = (
            "from gwflow import Task, Workflow\ngwf = Workflow()\ntask = Task(inputs=[])\n"
            f"target = task.target('compute', inputs=[], outputs=['out.txt'], image={reference!r})\n"
            "target << 'test -z \"$(ls -A .)\"; cat \"$APPTAINER_CONTAINER\" > out.txt'\n"
            "task.retain('result', source=target.output('out.txt'), path='out.txt')\n"
            "gwf.task_from_template('sample', task)\n"
        )
        if other:
            declaration += (
                "task = Task(inputs=[])\n"
                "target = task.target('compute', inputs=[], outputs=['out.txt'])\n"
                "target << 'printf independent > out.txt'\n"
                "task.retain('result', source=target.output('out.txt'), path='out.txt')\n"
                "gwf.task_from_template('independent', task)\n"
            )
        (self.work / "workflow.py").write_text(declaration)

    def options(self, **options):
        (self.controller / "options.json").write_text(json.dumps(options))

    def calls(self, operation):
        path = self.controller / "calls.jsonl"
        if not path.exists():
            return []
        return [call for call in map(json.loads, path.read_text().splitlines()) if call["operation"] == operation]

    def cached_images(self):
        # Locate fixture content without depending on the cache's private keys.
        return [path for path in self.cache.rglob("*") if path.is_file() and path.read_bytes() == b"fixture image\n"]


class RegistryImageTests(RegistryTestCase):
    def test_status_reports_pull_before_acquisition_finishes(self):
        self.options(gate="progress")
        progress = self.work / "pull-progress.log"
        with progress.open("w") as stream, subprocess.Popen(
            [GWF, "status"], cwd=self.work, stdout=subprocess.PIPE, stderr=stream, text=True,
        ) as process:
            try:
                self.wait_for(lambda: (self.controller / "progress-held").exists())
                self.assertIsNone(process.poll())
                self.assertEqual(progress.read_text(), f"Pulling image: {self.reference}\n")
                self.assertEqual(self.cached_images(), [])
            finally:
                (self.controller / "progress-release").touch()
            output, _ = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, output + progress.read_text())
        self.assertEqual(progress.read_text(),
                         f"Pulling image: {self.reference}\nPulled image: {self.reference}\n")
        self.assertNotIn("Pulling image:", output)
        self.assertNotIn("Pulled image:", output)
        self.assertEqual(len(self.cached_images()), 1)

    def test_status_acquires_missing_image_without_task_state(self):
        declaration = subprocess.run([sys.executable, "workflow.py"], cwd=self.work,
                                     capture_output=True, text=True, timeout=30)
        self.assertEqual(declaration.returncode, 0, declaration.stderr)
        self.assertEqual(self.calls("pull"), [])
        self.assertFalse(self.cache.exists())
        output = self.cli("status", "--details")
        self.assertNotIn("blocked", output)
        self.assertEqual([call["reference"] for call in self.calls("pull")], [self.reference])
        self.assertEqual(len(self.cached_images()), 1)
        self.assertEqual(self.calls("exec"), [])
        self.assertFalse((self.work / ".gwf/gwflow").exists())
        self.assertFalse((self.work / "work").exists())
        self.assertFalse((self.work / "results").exists())

    def test_each_planning_command_acquires_a_cold_reference(self):
        for index, arguments in enumerate((("status", "--details"), ("explain",), ("run", "--dry-run"), ("run",))):
            with self.subTest(arguments=arguments):
                # Distinct source sizes invalidate Python's timestamp/size
                # bytecode cache even when CI rewrites within one clock tick.
                reference = "docker://example.org/tool:v" + "1" * (index + 1)
                self.configure_workflow(reference)
                output = self.cli(*arguments)
                self.assertIn(f"Pulling image: {reference}", output)
                self.assertIn(f"Pulled image: {reference}", output)
                self.assertEqual(self.calls("pull")[-1]["reference"], reference)
                self.assertEqual(len(self.calls("pull")), index + 1)
                if arguments == ("run",):
                    self.finish()
                    self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")
                    image = Path(self.calls("exec")[-1]["image"])
                    self.assertTrue(image.is_absolute())
                    self.assertIn(image, self.cached_images())
                else:
                    self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_warm_cache_does_not_consult_registry_or_require_apptainer(self):
        self.run_complete()
        image = self.cached_images()[0]
        original = image.read_bytes(), image.stat().st_mtime_ns
        before = self.snapshot()
        self.options(fail=True, content="moved tag\n")
        executable = self.bin / "apptainer"
        executable.rename(self.bin / "hidden-apptainer")
        # No fallback to a deployment Apptainer installation during inspection.
        environment = {**os.environ, "PATH": str(self.bin)}
        for arguments in (("explain",), ("status", "--details"), ("run", "--dry-run"), ("run",)):
            output = self.cli(*arguments, env=environment)
            self.assertNotIn("blocked", output)
            self.assertNotIn("Submitted target", output)
            self.assertNotIn("Pulling image:", output)
            self.assertNotIn("Pulled image:", output)
            self.assertNotIn("Failed to pull image:", output)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertEqual((image.read_bytes(), image.stat().st_mtime_ns), original)

    def test_pull_failure_blocks_every_submission_and_can_retry(self):
        self.configure_workflow(other=True)
        self.options(fail=True)
        for arguments, success in ((("status",), True), (("explain",), True),
                                   (("run", "--dry-run"), False), (("run",), False)):
            result = subprocess.run([GWF, *arguments], cwd=self.work,
                                    capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode == 0, success, output)
            self.assertIn(f"Failed to pull image: {self.reference}", result.stderr)
            self.assertIn("Apptainer pull failed (exit 1): fixture registry unavailable", result.stderr)
            for diagnostic in ("sample", "compute", self.reference, "fixture registry unavailable", "independent"):
                self.assertIn(diagnostic, output)
            self.assertIn(f"Pulling image: {self.reference}", output)
            self.assertNotIn("Pulled image:", output)
            self.assertNotIn("Submitted target", output)
            self.assertFalse((self.work / ".gwf/gwflow").exists())
            self.assertFalse(self.cached_images())
        self.options()
        self.run_complete()
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")
        self.assertEqual((self.work / "results/independent/out.txt").read_text(), "independent")

    def test_missing_apptainer_blocks_cold_cache_only(self):
        environment = {**os.environ, "PATH": str(self.controller / "missing-bin")}
        output = self.cli("run", env=environment, success=False)
        self.assertIn("apptainer", output)
        self.assertIn(self.reference, output)
        self.assertIn(f"Failed to pull image: {self.reference}", output)
        self.assertNotIn("Pulled image:", output)
        self.assertNotIn("Submitted target", output)
        self.assertFalse(self.cached_images())

    def test_display_filter_still_acquires_hidden_task_image(self):
        self.configure_workflow(other=True)
        for index, command in enumerate(("status", "explain"), start=1):
            self.cli(command, "independent")
            self.assertEqual(len(self.calls("pull")), index)
            self.cached_images()[0].unlink()
        self.assertFalse((self.work / ".gwf/gwflow").exists())

    def test_digest_and_tag_references_have_distinct_shared_entries(self):
        references = (self.reference, "docker://example.org/tools/demo:other",
                      "docker://example.org/tools/demo@sha256:" + "a" * 64)
        for reference in references:
            self.configure_workflow(reference)
            self.cli("explain")
        self.assertEqual(len(self.cached_images()), 3)
        self.assertEqual([call["reference"] for call in self.calls("pull")], list(references))
        for reference in references:
            self.configure_workflow(reference)
            self.cli("status")
        self.assertEqual(len(self.calls("pull")), 3)

    def test_different_workflows_and_task_names_share_one_entry(self):
        self.run_complete()
        image = Path(self.calls("exec")[0]["image"])
        other = self.work / "second workflow"
        other.mkdir()
        (other / "workflow.py").write_text((self.work / "workflow.py").read_text().replace("'sample'", "'other'"))
        shutil.copyfile(self.work / ".gwfconf.json", other / ".gwfconf.json")
        result = subprocess.run([GWF, "run"], cwd=other, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.finish()
        self.assertEqual((other / "results/other/out.txt").read_text(), "fixture image\n")
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertEqual([call["image"] for call in self.calls("exec")], [str(image), str(image)])

    def test_default_and_relative_override_locations_are_absolute(self):
        # Isolate the CLI's user-home environment; never write the real user's cache.
        home = self.work / "user home"
        environment = dict(os.environ, HOME=str(home))
        environment.pop("GWFLOW_IMAGE_CACHE")
        self.cli("explain", env=environment)
        self.cache = home / ".cache/gwflow/images"
        self.assertEqual(len(self.cached_images()), 1)
        self.assertTrue(Path(self.calls("pull")[-1]["destination"]).is_relative_to(self.cache))
        environment["GWFLOW_IMAGE_CACHE"] = "relative cache"
        self.cli("explain", env=environment)
        self.cache = self.work / "relative cache"
        self.assertEqual(len(self.cached_images()), 1)
        self.assertTrue(Path(self.calls("pull")[-1]["destination"]).is_relative_to(self.cache))

    def test_invalid_cache_override_and_managed_locations_do_not_pull(self):
        unusable = self.work / "not-a-directory"
        unusable.touch()
        (self.work / "alias-to-work").symlink_to(self.work / "work")
        for cache in ("", str(unusable), "work/images", "results/images", ".gwf/images", "alias-to-work/images"):
            with self.subTest(cache=cache):
                environment = dict(os.environ, GWFLOW_IMAGE_CACHE=cache)
                output = self.cli("run", env=environment, success=False)
                self.assertRegex(output, "GWFLOW_IMAGE_CACHE|managed storage|Not a directory|File exists")
                self.assertEqual(self.calls("pull"), [])
                self.assertFalse((self.work / "work").exists())
                self.assertFalse((self.work / "results").exists())

    def test_present_unusable_entries_are_not_replaced(self):
        self.cli("explain")
        image = self.cached_images()[0]
        for unusable in ("directory", "broken symlink", "unreadable"):
            with self.subTest(unusable=unusable):
                image.unlink()
                if unusable == "directory":
                    image.mkdir()
                elif unusable == "broken symlink":
                    image.symlink_to(self.work / "absent.sif")
                else:
                    image.write_text("fixture image\n")
                    image.chmod(0)
                try:
                    output = self.cli("run", success=False)
                    self.assertIn("image unavailable", output)
                    self.assertEqual(len(self.calls("pull")), 1)
                finally:
                    if image.is_dir():
                        image.rmdir()
                    else:
                        image.unlink()
                    image.write_text("fixture image\n")

    def test_cleanup_and_fresh_attempts_preserve_cached_image(self):
        self.run_complete()
        image = self.cached_images()[0]
        info = image.stat()
        self.cli("clean-work", "--delete")
        self.assertEqual(image.stat(), info)
        self.assertNotIn("Submitted target", self.cli("run"))
        self.cli("run", "--force")
        self.settle()
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertEqual(len(self.calls("exec")), 2)
        self.assertEqual((image.stat().st_size, image.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))

    def test_reacquisition_for_cleaned_reuse_preserves_lifecycle_evidence(self):
        self.run_complete()
        self.cli("clean-work", "--delete")
        image = self.cached_images()[0]
        before = self.snapshot()
        image.unlink()
        self.cli("explain")
        self.assertEqual(len(self.calls("pull")), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("fresh attempt", self.cli("explain"))
        self.assertEqual((self.work / "results/sample/out.txt").read_text(), "fixture image\n")
