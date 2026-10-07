"""Representative frozen 25-Task, 418-job workload through the installed CLI.

Optional GWFLOW_PLANNING_BASELINES is a JSON map of labels to archived source
directories (each containing src/). Every version inspects the same frozen
workflow, using this checkout's probe and fixture backend. GWFLOW_IO_EVIDENCE
saves raw reports; no live registry, cluster or private data is required.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from support import FIXTURES, GWF
from test_registry_images import RegistryTestCase


class RepresentativePlanningTests(RegistryTestCase):
    workers = 8

    def configure_workflow(self):
        RegistryTestCase.configure_workflow(self)
        (self.work / "workflow.py").write_text(
            "from gwflow import Task, Workflow, shell\n"
            "gwf = Workflow()\n"
            "def chain(inputs, count):\n"
            "    task = Task(inputs=inputs)\n"
            "    incoming = inputs\n"
            "    for step in range(count):\n"
            f"        image = {self.reference!r} if step == 0 else None\n"
            "        target = task.target(f'step{step}', inputs=incoming, outputs=['value.txt'], image=image)\n"
            "        command = 'cat ' + ' '.join('{i' + str(i) + '}' for i in range(len(incoming))) + ' > value.txt'\n"
            "        target << shell(command, **{f'i{i}': source for i, source in enumerate(incoming)})\n"
            "        incoming = [target.output('value.txt')]\n"
            "    for index in range(2):\n"
            "        task.retain(f'value{index}', source=incoming[0], path=f'value{index}.txt')\n"
            "    return task\n"
            "reference = gwf.task_from_template('reference', chain(['input.txt'], 8))\n"
            "for index in range(24):\n"
            "    gwf.task_from_template(f'sample{index:02}', chain(list(reference.outputs.values()), 15))\n"
        )

    def wait_for(self, predicate):
        # This fixture runs 418 actual jobs; ordinary small fixtures keep their
        # shorter deadline. Completion is still the predicate, never a sleep.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Timed out preparing the 418-job planning fixture")

    def command(self, arguments, environment, *, probe=None):
        executable = ([sys.executable, str(FIXTURES / "planning_probe.py"), str(probe)]
                      if probe else [GWF])
        started = time.perf_counter()
        result = subprocess.run([*executable, "-b", "recovery_fixture", *arguments], cwd=self.work,
                                env=environment, text=True, capture_output=True, timeout=300)
        elapsed = time.perf_counter() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout, elapsed

    def frozen_state(self):
        digest = hashlib.sha256()
        for root in (self.work / ".gwf/gwflow", self.work / "work", self.work / "results", self.cache,
                     self.work / "input.txt"):
            for path in ([root] if root.is_file() else sorted(root.rglob("*"))):
                if path.is_file():
                    info = path.stat()
                    digest.update(str(path.relative_to(self.work)).encode())
                    digest.update(str((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)).encode())
                    digest.update(path.read_bytes())
        return digest.hexdigest()

    def test_combined_workload_counts_and_command_behavior(self):
        environment = self.inject(record_acceptances=True)
        print(f"\nPreparing 418 local jobs in {self.work}", flush=True)
        self.command(["run"], environment)
        self.finish()
        print("All 418 jobs completed; freezing observations", flush=True)
        self.assertEqual(len((self.work / "backend-acceptances.jsonl").read_text().splitlines()), 418)
        self.assertEqual(len(self.calls("pull")), 1)
        self.cli("clean-work", "--delete", "--task", "reference")
        self.assertFalse(list((self.work / "work/reference").glob("*/step*")))
        states = {}
        for index in range(20, 24):
            states[f"sample{index:02}__step14"] = "RUNNING" if index < 22 else "SUBMITTED"
            states[f"sample{index:02}__gwflow_complete"] = "SUBMITTED"
        environment = self.inject(job_states=states)
        frozen = self.frozen_state()
        image, = self.cached_images()
        versions = json.loads(os.environ.get("GWFLOW_PLANNING_BASELINES", "{}"))
        versions["optimized"] = None
        evidence = {"jobs": 418, "tasks": 25, "frozen_state": frozen, "versions": {},
                    "filesystem": {"device": self.work.stat().st_dev, "inode": self.work.stat().st_ino}}
        commands = (("status",), ("status", "--details"), ("status", "sample00"),
                    ("status", "--status", "running"), ("explain",), ("run", "--dry-run"), ("run",))
        outputs, environments = {}, {}
        for label, source in versions.items():
            print(f"Measuring {label}", flush=True)
            current = dict(environment)
            if source is not None:
                current["PYTHONPATH"] = str(Path(source).resolve() / "src") + os.pathsep + str(self.work)
            environments[label] = current
            records = {}
            for arguments in commands:
                name = "-".join(arguments)
                report = self.work / "probe.json"
                output, _ = self.command(arguments, current, probe=report)
                records[name] = json.loads(report.read_text())
                if destination := os.environ.get("GWFLOW_IO_EVIDENCE"):
                    root = Path(destination)
                    root.mkdir(parents=True, exist_ok=True)
                    (root / f"{label}-{name}.json").write_text(report.read_text())
                ordinary, _ = self.command(arguments, current)
                self.assertEqual(output, ordinary)
                if name in outputs:
                    self.assertEqual(output, outputs[name], (label, name))
                else:
                    outputs[name] = output
                self.assertEqual(self.frozen_state(), frozen)
            evidence["versions"][label] = {"reports": records, "wall_seconds": []}
        # Interleave versions so changes in host/import load do not coincide
        # systematically with one implementation's timing samples.
        for repeat in range(3):
            print(f"Timing round {repeat + 1}", flush=True)
            for label, current in environments.items():
                output, seconds = self.command(("status",), current)
                self.assertEqual(output, outputs["status"])
                evidence["versions"][label]["wall_seconds"].append(seconds)
            if destination := os.environ.get("GWFLOW_IO_EVIDENCE"):
                root = Path(destination)
                root.mkdir(parents=True, exist_ok=True)
                (root / "representative.json").write_text(json.dumps(evidence, indent=2) + "\n")
        self.assertIn("25 Tasks shown", outputs["status"])
        self.assertIn("21 reusable", outputs["status"])
        self.assertIn("2 active", outputs["status"])
        self.assertRegex(outputs["status"], r"Task sample20\s+running\s+15/17")
        self.assertIn("2 queued", outputs["status"])
        self.assertEqual(len(self.calls("pull")), 1)
        self.assertEqual(self.frozen_state(), frozen)
        # Per-evidence bounds are independent of machine timing or path depth.
        for report in evidence["versions"]["optimized"]["reports"].values():
            counts = report["counts"]["planning"]
            paths = report["paths"]["planning"]
            producer = {path: count for path, count in paths.items()
                        if path.startswith("read:") and "/tasks/reference/" in path}
            self.assertEqual(max(producer.values()), 1)
            self.assertEqual(paths["stat:" + str(image)], 1)
            self.assertEqual(paths["access:" + str(image)], 1)
            self.assertLessEqual(report["peak_opened_descriptors"], 67)
            self.assertEqual(report["counts"]["backend"]["scheduler_requests"], 1)
            self.assertLessEqual(paths["directory_opens:/"], 8)
            # Controlled depth-2 measurements: 2703 directory opens, 3101
            # file opens, 2565 reads and 84119 stats. Allow modest fixed
            # headroom and explicit ancestry/eviction work at greater depth.
            extra_depth = max(0, report["path_depth"] - 2)
            self.assertLessEqual(counts["directory_opens"], 3200 + 418 * extra_depth)
            self.assertLessEqual(counts["file_opens"], 3300)
            self.assertLessEqual(counts["stream_reads"], 2700)
            self.assertLessEqual(counts["stat"], 90000 + 6000 * extra_depth)
        if "unoptimized" in evidence["versions"]:
            baseline = evidence["versions"]["unoptimized"]["reports"]["status"]
            self.assertGreater(baseline["counts"]["planning"]["file_opens"], 3300)
            self.assertGreater(baseline["paths"]["planning"]["directory_opens:/"], 8)
