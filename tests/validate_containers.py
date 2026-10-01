"""Opt-in public-CLI acceptance on real local workers or Slurm; preserves evidence."""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from gwf.backends.local import Client, LocalStatus

from support import GWF, LocalBackendTestCase
from test_containers import ContainerRuntimeTests
from test_staging import ContainerStagingTests
from test_container_graphs import ContainerGraphTests
from test_container_environment import ContainerScratchTests, write_workflow
from test_container_recovery import ContainerRecoveryTests
from test_example import PackagedContainerExampleTests


class DeploymentTests(LocalBackendTestCase):
    def configure_workflow(self):
        program = (
            "import importlib.metadata as m,json,platform,shutil,subprocess,sys; "
            "print(json.dumps({'node':platform.node(),'python':sys.version,"
            "'packages':{n:m.version(n) for n in ('gwf','gwflow','gwflow-summary-task','gwflow-report-task')},"
            "'apptainer_path':shutil.which('apptainer'),"
            "'apptainer':subprocess.check_output(['apptainer','--version'],text=True).strip()}))"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} > runtime.json"
        (self.work / "workflow.py").write_text(
            "from gwflow import Task,Workflow\ngwf = Workflow()\ntask = Task(inputs=[])\n"
            "probe = task.target('probe',inputs=[],outputs=['runtime.json'])\n"
            f"probe << {command!r}\n"
            "task.retain('runtime',source=probe.output('runtime.json'),path='runtime.json')\n"
            "gwf.task_from_template('deployment',task)\n"
        )

    def test_job_uses_the_installed_validation_environment(self):
        self.run_complete()
        result = json.loads((self.work / "results/deployment/runtime.json").read_text())
        self.assertIn("1.5.4", result["apptainer"])
        self.assertTrue(result["python"].startswith("3.12."))
        for name, version in result["packages"].items():
            self.assertEqual(version, importlib.metadata.version(name))


CASES = {
    "deployment": (DeploymentTests, "test_job_uses_the_installed_validation_environment"),
    "image": (ContainerRuntimeTests, "test_image_software_runs_with_private_scratch_and_reuses_after_cleanup"),
    "overlap": (ContainerStagingTests, "test_source_parent_with_private_work_is_readonly_but_work_and_scratch_are_writable"),
    "aliases": (ContainerStagingTests, "test_alias_through_linked_parent_stages_basename_with_shell_punctuation"),
    "companions": (ContainerStagingTests, "test_custom_paths_disambiguate_basenames_and_place_declared_companions_together"),
    "mixed": (ContainerGraphTests, "test_host_and_different_container_targets_exchange_checked_outputs"),
    "refresh": (ContainerGraphTests, "test_image_change_refreshes_whole_task_and_consumers_with_equal_retained_metadata"),
    "environment": (ContainerScratchTests, "test_image_settings_and_explicit_overrides_survive_clean_environment"),
    "scratch": (ContainerScratchTests, "test_concurrent_managed_scratch_is_private_persistent_and_follows_work_placement"),
    "opt_out": (ContainerScratchTests, "test_opt_out_forwards_job_tmpdir_and_preserves_external_scratch_after_cleanup"),
    "retry": (ContainerRecoveryTests, "test_ordinary_retry_preserves_successful_branch_and_uses_fresh_private_storage"),
    "repair": (ContainerRecoveryTests, "test_unavailable_image_blocks_repair_and_changed_image_requires_fresh_computation"),
    "packaged": (PackagedContainerExampleTests, "test_packaged_containers_clean_producers_then_compute_only_the_new_consumer"),
}


class RuntimeBackendCase(LocalBackendTestCase):
    """Run existing behavioral assertions with real scheduling and durable transcripts."""

    def setUp(self):
        self.work = self.root / self.case_name
        self.work.mkdir()
        self.job_environment = dict(os.environ)
        # Authored test traces and deployment fault files live outside private work.
        # /tmp tests see them via the default temporary mount; shared storage needs
        # an explicit ordinary site bind. Requested source mounts must still be ro.
        if self.case_name in ("mixed", "refresh", "retry", "repair"):
            inherited_binds = self.job_environment.get("APPTAINER_BINDPATH", "")
            self.job_environment["APPTAINER_BINDPATH"] = ",".join(filter(None, (inherited_binds, str(self.work))))
        self.jobs = set()
        (self.work / "input.txt").write_text("hello\n")
        (self.work / "extra.txt").write_text("extra\n")
        self.configure_workflow()
        self.config = {"backend": self.backend}
        if self.backend == "local":
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]
            self.config["backend.local.port"] = self.port
        self.configure()
        self.record({"case": self.case_name, "environment": {
            name: value for name, value in self.job_environment.items()
            if name in ("PATH", "PYTHONPATH", "TMPDIR", "GWFLOW_IMAGE_VALUE", "GWFLOW_TEST_OVERRIDE",
                        "APPTAINER_BIND", "APPTAINER_BINDPATH", "APPTAINER_NO_MOUNT",
                        "APPTAINER_CONFIGDIR", "APPTAINER_CACHEDIR", "APPTAINERENV_TMPDIR",
                        "APPTAINERENV_GWFLOW_TEST_OVERRIDE", "SINGULARITY_BIND", "SINGULARITY_BINDPATH",
                        "SINGULARITYENV_TMPDIR", "SINGULARITYENV_GWFLOW_TEST_OVERRIDE")
        }})
        if self.backend == "local":
            worker = subprocess.Popen(
                [GWF, "workers", "--host", "127.0.0.1", "-n", "2", "-p", str(self.port)],
                cwd=self.work, env=self.job_environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            self.addCleanup(self.stop_worker, worker)
            self.wait_for(lambda: self.worker_ready(worker))

    def record(self, item):
        with (self.work / "transcript.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": time.time(), **item}) + "\n")

    def cli(self, *args, success=True, env=None):
        self.assertNotIn("recovery_fixture", args, "Live acceptance cannot select a fixture backend")
        workflow = self.work / "workflow.py"
        if self.backend == "slurm":
            # These selected fixture workflows have one explicit Workflow construction.
            source = workflow.read_text()
            if "Workflow(defaults=" not in source:
                source = source.replace("Workflow(", f"Workflow(defaults={self.resources!r}, ")
                workflow.write_text(source)
        images = {}
        for path in self.work.rglob("*.sif"):
            if path.is_file():
                info = path.stat()
                images[str(path)] = {"resolved": str(path.resolve()), "size": info.st_size,
                                     "mtime_ns": info.st_mtime_ns}
        result = subprocess.run([GWF, *args], cwd=self.work, env=env or self.job_environment,
                                capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        self.jobs.update(re.findall(r"Backend job: (\d+)", output))
        self.record({"command": [GWF, *args], "exit": result.returncode,
                     "output": output, "images": images})
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        if "Submitted target" in output:
            self.cli("status", "--details", env=env)
        return output

    def wait_for(self, predicate):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(1 if self.backend == "local" else 3)
        self.fail(f"Timed out; inspect {self.work}. No jobs were cancelled by this runner.")

    def settle(self):
        def inactive():
            if self.backend == "local":
                with Client.connect(port=self.port) as client:
                    states = client.status()
                self.record({"local_states": {str(key): str(value) for key, value in states.items()}})
                return states and not any(state in (LocalStatus.SUBMITTED, LocalStatus.RUNNING)
                                          for state in states.values())
            self.assertTrue(self.jobs, "No Slurm job IDs found in CLI submissions")
            result = subprocess.run(
                ["sacct", "-X", "-n", "-P", "-j", ",".join(sorted(self.jobs)),
                 "--format=JobIDRaw,JobName%100,State,ExitCode,NodeList"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            self.record({"slurm_accounting": result.stdout})
            states = {row.split("|")[0]: row.split("|")[2].split()[0]
                      for row in result.stdout.splitlines() if row}
            self.last_states = states
            terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL"}
            return self.jobs <= states.keys() and all(states[job] in terminal for job in self.jobs)
        self.wait_for(inactive)

    def finish(self):
        if self.backend == "local":
            return LocalBackendTestCase.finish(self)
        self.settle()
        self.assertTrue(all(state == "COMPLETED" for state in self.last_states.values()), self.last_states)

    def check_slurm_job_scratch(self):
        # Slurm may replace the submitter's TMPDIR with a job-local directory.
        # Observe the same job immediately around its real Apptainer invocation.
        executable = shutil.which("apptainer", path=self.job_environment["PATH"])
        probe = self.work / "launch probe"
        probe.mkdir()
        wrapper = probe / "apptainer"
        host_path, host_content = self.work / "host-tmpdir", self.work / "host-scratch-content"
        marker = "gwflow-validation-scratch"
        wrapper.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"${{TMPDIR-unset}}\" > {shlex.quote(str(host_path))}\n"
            f"{shlex.quote(executable)} \"$@\"\nresult=$?\n"
            f"if [ \"$result\" = 0 ]; then cat \"$TMPDIR/{marker}\" > {shlex.quote(str(host_content))}; fi\n"
            "exit \"$result\"\n"
        )
        wrapper.chmod(0o755)
        self.job_environment["PATH"] = str(probe) + os.pathsep + self.job_environment["PATH"]
        command = f'echo external > "$TMPDIR/{marker}"; printf "%s\\n" "$TMPDIR" > out.txt'
        write_workflow(self.work, [("sample", command, True)], managed=False)
        self.run_complete()
        selected = (self.work / "results/sample/result.txt").read_text()
        self.assertEqual(selected, host_path.read_text())
        self.assertNotEqual(selected, "unset\n")
        self.assertFalse(Path(selected.strip()).is_relative_to(self.work / "work"))
        self.assertEqual(host_content.read_text(), "external\n")
        self.cli("clean-work", "--delete")
        self.assertNotIn("Submitted target", self.cli("run"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("local", "slurm"), required=True)
    parser.add_argument("--root", type=Path, required=True, help="New evidence directory on job-visible storage")
    parser.add_argument("--queue", default="short", help="Site Slurm partition (gwf queue option)")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per wait; no automatic cancellation")
    parser.add_argument("--case", action="append", choices=CASES, help="Run selected cases; default is all")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or importlib.metadata.version("gwf") != "2.1.1":
        parser.error("Use Python 3.12 and pinned gwf 2.1.1")
    if not shutil.which("apptainer"):
        parser.error("Deployment-provided Apptainer must be on PATH")
    version = subprocess.check_output(["apptainer", "--version"], text=True).strip()
    if not re.search(r"\b1\.5\.4(?:\b|-)", version):
        parser.error(f"The acceptance reference is Apptainer 1.5.4; found {version}")
    for name in ("GWFLOW_TEST_SIF", "GWFLOW_TEST_SUMMARY_SIF", "GWFLOW_TEST_REPORT_SIF"):
        if not Path(os.environ.get(name, "")).is_file():
            parser.error(f"Set {name} to a prepared SIF; missing runtime evidence is not a pass")
    if args.backend == "slurm" and not all(shutil.which(name) for name in ("sbatch", "sacct")):
        parser.error("Real Slurm commands must be available")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    scratch = root / "fixture temporary files"
    scratch.mkdir()
    tempfile.tempdir = str(scratch)
    metadata = {"backend": args.backend, "platform": platform.platform(), "python": sys.version,
                "apptainer": version, "apptainer_path": shutil.which("apptainer"),
                "packages": {name: importlib.metadata.version(name) for name in
                             ("gwf", "gwflow", "gwflow-summary-task", "gwflow-report-task")}}
    metadata["images"] = {}
    for name in ("GWFLOW_TEST_SIF", "GWFLOW_TEST_SUMMARY_SIF", "GWFLOW_TEST_REPORT_SIF"):
        path = Path(os.environ[name]).resolve()
        info = path.stat()
        metadata["images"][name] = {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    (root / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    suite = unittest.TestSuite()
    selected_cases = args.case or list(CASES)
    for name in selected_cases:
        original, method = CASES[name]
        case = type(f"Live_{name}", (original, RuntimeBackendCase), {
            "root": root, "backend": args.backend, "case_name": name, "timeout": args.timeout,
            "resources": {"queue": args.queue, "cores": 1, "memory": "256m", "walltime": "00:02:00"},
            "settle": RuntimeBackendCase.settle,
        })
        if args.backend == "slurm" and name == "opt_out":
            method = "test_job_tmpdir_is_forwarded_without_becoming_managed_work"
            setattr(case, method, RuntimeBackendCase.check_slurm_job_scratch)
        suite.addTest(case(method))
    result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
    outcomes = {name: "passed" for name in selected_cases[:result.testsRun]}
    for status, entries in (("failed", result.failures), ("error", result.errors), ("skipped", result.skipped)):
        for case, _ in entries:
            outcomes[case.case_name] = status
    (root / "summary.json").write_text(json.dumps({
        "run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skips": len(result.skipped), "cases": selected_cases, "outcomes": outcomes,
    }, indent=2) + "\n")
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    sys.exit(main())
