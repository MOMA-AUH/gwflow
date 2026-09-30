"""Run the small-file cleanup scenario against an installed gwflow package."""

import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from gwf.backends.local import Client, LocalStatus


GWF = str(Path(sys.executable).with_name("gwf"))


def wait_for(predicate, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {description}")


def run(work):
    result = subprocess.run(
        [GWF, "run"], cwd=work, capture_output=True, text=True, timeout=30
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout + result.stderr


def main():
    with tempfile.TemporaryDirectory(prefix="gwflow-release-") as directory:
        work = Path(directory)
        (work / "workflow.py").write_text(
            "from gwflow import Task, Workflow\n"
            "gwf = Workflow()\n"
            "task = Task(inputs=[])\n"
            "target = task.target('write', inputs=[], outputs=['out.txt'])\n"
            "target << 'printf HELLO > out.txt'\n"
            "task.retain('text', source=target.output('out.txt'), path='text.txt')\n"
            "gwf.task_from_template('alpha', task)\n"
            "gwf.task_from_template('beta', task)\n"
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        (work / ".gwfconf.json").write_text(
            json.dumps({"backend": "local", "backend.local.port": port})
        )
        worker = subprocess.Popen(
            [GWF, "workers", "-n", "2", "-p", str(port)],
            cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            def worker_ready():
                if worker.poll() is not None:
                    raise RuntimeError(f"gwf workers exited: {worker.stderr.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        return True
                except OSError:
                    return False

            wait_for(worker_ready, "local workers")
            first = run(work)
            if "Submitted target alpha__gwflow_complete" not in first:
                raise AssertionError(first)

            def jobs_complete():
                with Client.connect(port=port) as client:
                    states = client.status().values()
                if LocalStatus.FAILED in states:
                    raise RuntimeError("A local job failed")
                return bool(states) and all(state == LocalStatus.COMPLETED for state in states)

            wait_for(jobs_complete, "task completion")
            expected = "HELLO"
            for name in ("alpha", "beta"):
                if (work / "results" / name / "text.txt").read_text() != expected:
                    raise AssertionError(f"Unexpected {name} output")
            shutil.rmtree(work / "work")
            second = run(work)
            if "Submitted target" in second:
                raise AssertionError(second)
            if (work / "work").exists():
                raise AssertionError("An internal intermediate was recreated")
            print("Published-package cleanup and reuse smoke check passed")
        finally:
            worker.terminate()
            try:
                worker.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate(timeout=10)


if __name__ == "__main__":
    main()
