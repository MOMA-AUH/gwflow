"""Container environment and scratch ownership through real scheduled commands."""

import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from support import TASK_FACTORY, LocalBackendTestCase


def write_workflow(work, commands, *, managed=True, work_root="work"):
    declaration = TASK_FACTORY + f"from gwflow import Task, Workflow\ngwf = Workflow(managed_tmpdir={managed!r}, work_root={work_root!r})\n"
    for name, command, container in commands:
        image = os.environ["GWFLOW_TEST_SIF"] if container else None
        declaration += (
            "task = empty_task(inputs=[])\n"
            f"target = task.target('compute', inputs=[], outputs=['out.txt'], image={image!r})\n"
            f"target << {command!r}\n"
            "task.retain('value', source=target.output('out.txt'), path='result.txt')\n"
            f"gwf.task(task, alias={name!r})\n"
        )
    (work / "workflow.py").write_text(declaration)


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerScratchTests(LocalBackendTestCase):
    def setUp(self):
        external = tempfile.TemporaryDirectory(prefix="gwflow deployment scratch ")
        self.addCleanup(external.cleanup)
        self.external = Path(external.name)
        with patch.dict(os.environ, {"TMPDIR": str(self.external),
                                     "PYTHONPATH": "/host/environment/pollution",
                                     "GWFLOW_IMAGE_VALUE": "host pollution",
                                     "GWFLOW_TEST_OVERRIDE": "ordinary host value",
                                     "APPTAINERENV_GWFLOW_TEST_OVERRIDE": "explicit $value 'quoted'",
                                     "APPTAINERENV_TMPDIR": "/incorrect-deployment-override",
                                     "APPTAINER_NO_MOUNT": "tmp"}):
            super().setUp()

    def configure_workflow(self):
        write_workflow(self.work, [("sample", 'printf "%s\\n" "$TMPDIR" > out.txt', True)])

    def test_opt_out_forwards_job_tmpdir_and_preserves_external_scratch_after_cleanup(self):
        command = (f'test "$TMPDIR" = {shlex.quote(str(self.external))}; '
                   'echo external > "$TMPDIR/fixed-name"; printf "%s\\n" "$TMPDIR" > out.txt')
        write_workflow(self.work, [("sample", command, True)], managed=False)
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), str(self.external) + "\n")
        self.assertEqual((self.external / "fixed-name").read_text(), "external\n")
        self.cli("clean-work", "--delete")
        self.assertEqual((self.external / "fixed-name").read_text(), "external\n")
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_concurrent_managed_scratch_is_private_persistent_and_follows_work_placement(self):
        commands = []
        for name in ("left", "right"):
            command = (f'printf {name} > "$TMPDIR/fixed-name"; printf "%s" "$TMPDIR" > ready; '
                       'while [ ! -e release ]; do sleep 0.025; done; '
                       'cat "$TMPDIR/fixed-name" > out.txt')
            commands.append((name, command, True))
        write_workflow(self.work, commands, work_root="execution work")
        root = self.work / "execution work"
        self.cli("run")
        try:
            self.wait_for(lambda: len(list(root.rglob("ready"))) == 2)
            scratch = [Path(path.read_text()) for path in root.rglob("ready")]
            self.assertEqual(len(set(scratch)), 2)
            self.assertTrue(all(path.is_relative_to(root) for path in scratch))
            self.assertCountEqual([(path / "fixed-name").read_text() for path in scratch], ["left", "right"])
            self.assertFalse((self.external / "fixed-name").exists())
        finally:
            for path in root.rglob("ready"):
                (path.parent / "release").touch()
        self.finish()
        for name in ("left", "right"):
            self.assertEqual((self.work / "results" / name / "result.txt").read_text(), name)
        self.assertTrue(all((path / "fixed-name").is_file() for path in scratch))
        self.cli("clean-work")
        self.assertTrue(all(path.exists() for path in scratch))
        self.cli("clean-work", "--delete")
        self.assertTrue(all(not path.exists() for path in scratch))
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_image_settings_and_explicit_overrides_survive_clean_environment(self):
        command = ('printf "%s\\n" "$GWFLOW_IMAGE_VALUE" "$GWFLOW_TEST_OVERRIDE" '
                   '"${PYTHONPATH-unset}" "$TMPDIR" > out.txt')
        write_workflow(self.work, [("container", command, True), ("host", command, False)])
        self.run_complete()
        container = (self.work / "results/container/result.txt").read_text().splitlines()
        host = (self.work / "results/host/result.txt").read_text().splitlines()
        self.assertEqual(container[:3], ["from-image", "explicit $value 'quoted'", "unset"])
        self.assertEqual(host[:3], ["host pollution", "ordinary host value", "/host/environment/pollution"])
        for result in (container, host):
            self.assertTrue(Path(result[3]).is_relative_to(self.work / "work"))
            self.assertTrue(Path(result[3]).is_dir())


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerDefaultScratchTests(LocalBackendTestCase):
    def setUp(self):
        system = tempfile.TemporaryDirectory(prefix="gwflow ordinary tmp ", dir="/tmp")
        self.addCleanup(system.cleanup)
        self.system = Path(system.name)
        with patch.dict(os.environ):
            for name in ("TMPDIR", "APPTAINERENV_TMPDIR", "SINGULARITYENV_TMPDIR", "APPTAINER_NO_MOUNT"):
                os.environ.pop(name, None)
            super().setUp()

    def configure_workflow(self):
        write_workflow(self.work, [("sample", "touch out.txt", True)])

    def test_unset_opt_out_uses_container_defaults_and_keeps_system_temporary_files(self):
        command = ('printf "%s\\n" "${TMPDIR-unset}" > out.txt; '
                   "python -c 'import tempfile; print(tempfile.gettempdir())' >> out.txt; "
                   f'echo defaults > {shlex.quote(str(self.system / "fixed-name"))}')
        write_workflow(self.work, [("sample", command, True)], managed=False)
        self.run_complete()
        self.assertEqual((self.work / "results/sample/result.txt").read_text(), "unset\n/tmp\n")
        self.cli("clean-work", "--delete")
        self.assertEqual((self.system / "fixed-name").read_text(), "defaults\n")
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_hardcoded_system_temporary_files_are_outside_managed_cleanup(self):
        command = ('echo managed > "$TMPDIR/fixed-name"; printf "%s" "$TMPDIR" > out.txt; '
                   f'echo system > {shlex.quote(str(self.system / "fixed-name"))}')
        write_workflow(self.work, [("sample", command, True)])
        self.run_complete()
        scratch = Path((self.work / "results/sample/result.txt").read_text())
        self.assertTrue(scratch.is_relative_to(self.work / "work"))
        self.assertEqual((scratch / "fixed-name").read_text(), "managed\n")
        self.assertEqual((self.system / "fixed-name").read_text(), "system\n")
        self.cli("clean-work", "--delete")
        self.assertFalse(scratch.exists())
        self.assertEqual((self.system / "fixed-name").read_text(), "system\n")
        self.assertNotIn("Submitted target", self.cli("run"))
