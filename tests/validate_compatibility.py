"""Compare independent installed packages through unchanged public workflows."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

from gwf.backends.local import Client, LocalStatus

from support import FIXTURES, LocalBackendTestCase
from test_managed_recovery import ManagedCoordinationTests


class CompletedCompatibilityTests(LocalBackendTestCase):
    def inject(self, **options):
        ManagedCoordinationTests.inject(self, **options)
        return {**self.environment, "PYTHONPATH": str(self.work)}

    def setUp(self):
        self.select_installation("baseline")
        super().setUp()

    def make_workdir(self):
        work = self.root / self._testMethodName
        work.mkdir()
        return work

    def configure_workflow(self):
        shutil.copy(FIXTURES / "compatibility_workflow.py", self.work / "workflow.py")

    def select_installation(self, side):
        self.side = side
        self.executable = str(Path(self.installations[side]["executable"]).with_name("gwflow"))
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in ("PYTHONPATH", "PYTHONHOME", "GWFLOW_COMPAT_CONSUMER", "GWFLOW_COMPAT_UNOWNED")}
        self.environment["PATH"] = str(Path(self.executable).parent) + os.pathsep + self.environment.get("PATH", "")

    def start_worker(self):
        return super().start_worker(env=self.environment)

    def record(self, event):
        with (self.root / "transcript.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": time.time(), "side": self.side, **event}) + "\n")

    def cli_result(self, *args, success=True, env=None):
        result = super().cli_result(*args, success=success, env=env or self.environment)
        self.record({"command": result.args, "cwd": str(self.work), "exit": result.returncode,
                     "stdout": result.stdout, "stderr": result.stderr})
        return result

    def snapshot(self):
        return {str(path.relative_to(self.work)): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
        } for directory in (".gwf/gwflow", "results")
                for path in (self.work / directory).rglob("*") if path.is_file()}

    def test_completed_cleaned_and_recovered_tasks_reuse_before_new_consumer(self):
        names = ("completed", "cleaned", "recovered")
        workflow = (self.work / "workflow.py").read_bytes()
        rejected = self.cli("-b", "recovery_fixture", "run", "recovered",
                            env=self.inject(reject_prefix="recovered__gwflow_prepare"), success=False)
        self.assertIn("injected submission failure", rejected)
        self.assertIn("Resolved submission", self.cli("recover", "recovered"))
        self.run_complete()
        self.cli("clean", "--delete", "cleaned")
        attempts = self.task_attempts()
        for name in names:
            self.assertEqual((self.work / "results" / name / "text.txt").read_text(), "hello\n")
            self.assert_runtime(name, "baseline")
        self.assertEqual(self.trace(), Counter({name: 1 for name in names}))
        self.assertFalse(list((self.work / "work/cleaned").iterdir()))
        self.assertTrue(list((self.work / "work/completed").iterdir()))
        records = list((self.work / ".gwf/gwflow").rglob("recovery.json"))
        self.assertEqual(len(records), 1)
        before = self.snapshot()
        identities = {str(path): {"device": path.stat().st_dev, "inode": path.stat().st_ino}
                      for path in (self.work, self.work / "work", self.work / "results", self.work / ".gwf/gwflow")}
        self.finish()
        with Client.connect(port=self.port) as client:
            states = client.status()
        self.assertTrue(states and all(state == LocalStatus.COMPLETED for state in states.values()), states)
        self.stop_worker(self.worker)
        self.assertIsNotNone(self.worker.poll())
        self.record({"baseline_jobs_completed": True, "worker_exit": self.worker.returncode,
                     "job_states": {str(job): state.name for job, state in states.items()},
                     "attempts": attempts, "storage": identities, "snapshot": before})

        self.select_installation("candidate")
        self.worker = self.start_worker()
        self.cli("status", "--details")
        preview = self.cli("run", "--dry-run")
        for name in names:
            self.assertRegex(preview, rf"Task {name}\s+Reuse\s+")
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertIn("No unresolved submissions", self.cli("recover", "recovered"))
        self.assertEqual(self.task_attempts(), attempts)
        self.assertEqual(self.snapshot(), before)
        environment = {**self.environment, "GWFLOW_COMPAT_CONSUMER": "1"}
        output = self.cli("run", "consumer", env=environment)
        self.finish()
        self.assertIn("Submitted target consumer__join", output)
        for name in names:
            self.assertNotIn(f"Submitted target {name}__", output)
        self.assertEqual((self.work / "results/consumer/text.txt").read_text(), "hello\nhello\nhello\n")
        self.assert_runtime("consumer", "candidate")
        self.assertEqual(self.trace(), Counter({**{name: 1 for name in names}, "consumer": 1}))
        self.assertFalse(list((self.work / "work/cleaned").iterdir()))
        self.assertEqual((self.work / "workflow.py").read_bytes(), workflow)
        for path, expected in identities.items():
            self.assertEqual({"device": Path(path).stat().st_dev, "inode": Path(path).stat().st_ino}, expected)
        after = self.snapshot()
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertNotIn("Submitted target", self.cli("run", env=environment))
        unowned = self.work / "results/unowned/text.txt"
        unowned.parent.mkdir()
        unowned.write_text("not managed")
        self.assertIn("existing files are not adopted", self.cli("run", "unowned", success=False,
                                                                env={**self.environment, "GWFLOW_COMPAT_UNOWNED": "1"}))
        self.assertEqual(unowned.read_text(), "not managed")
        self.assertEqual(self.trace(), Counter({**{name: 1 for name in names}, "consumer": 1}))
        self.record({"unchanged_workflow_sha256": hashlib.sha256(workflow).hexdigest(),
                     "same_storage": identities, "preserved_baseline_files": len(before),
                     "computation_counts": dict(self.trace()), "retained_consumer": "hello\nhello\nhello\n",
                     "unowned_results_preserved": True})

    def assert_runtime(self, task, side):
        runtime = json.loads((self.work / "results" / task / "runtime.json").read_text())
        self.assertEqual(runtime["prefix"], self.installations[side]["prefix"])
        self.assertTrue(Path(runtime["package"]).is_relative_to(runtime["prefix"]))
        self.record({"task": task, "runtime": runtime})


def installed_environment(python, source):
    program = """
import hashlib, importlib.metadata as m, json, pathlib, platform, sys
import gwflow
package = pathlib.Path(gwflow.__file__).parent
digest = hashlib.sha256()
for path in sorted(package.rglob('*.py')):
    digest.update(str(path.relative_to(package)).encode())
    digest.update(path.read_bytes())
distribution = m.distribution('gwflow')
print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix,
    'python': sys.version, 'platform': platform.platform(), 'package': str(package),
    'package_sha256': digest.hexdigest(),
    'versions': {name: m.version(name) for name in ('gwflow', 'gwf', 'rich')},
    'direct_url': json.loads(distribution.read_text('direct_url.json') or 'null')}))
"""
    result = json.loads(subprocess.check_output([str(python), "-I", "-c", program], text=True))
    if not Path(result["package"]).resolve().is_relative_to(Path(result["prefix"]).resolve()):
        raise ValueError(f"gwflow must be independently installed inside {result['prefix']}")
    if not result["python"].startswith("3.12.") or result["versions"]["gwf"] != "2.1.1":
        raise ValueError("Use Python 3.12 and gwf 2.1.1 for both installations")
    return {"source": source, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", type=Path, required=True)
    parser.add_argument("--candidate-python", type=Path, required=True)
    parser.add_argument("--baseline-source", required=True, help="Exact baseline source commit or artifact identifier")
    parser.add_argument("--candidate-source", required=True, help="Exact candidate source commit or artifact identifier")
    parser.add_argument("--root", type=Path, required=True, help="New directory for persistent storage and raw evidence")
    args = parser.parse_args()
    installations = {side: installed_environment(getattr(args, side + "_python"), getattr(args, side + "_source"))
                     for side in ("baseline", "candidate")}
    if Path(installations["baseline"]["prefix"]).samefile(installations["candidate"]["prefix"]):
        parser.error("Baseline and candidate must be independently installed in distinct environments")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "environment.json").write_text(json.dumps(installations, indent=2) + "\n")
    case = type("InstalledCompatibility", (CompletedCompatibilityTests,), {"root": root, "installations": installations})
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    (root / "summary.json").write_text(json.dumps({"run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skips": len(result.skipped)}, indent=2) + "\n")
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    sys.exit(main())
