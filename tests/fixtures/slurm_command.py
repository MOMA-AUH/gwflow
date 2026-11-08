"""Controlled Slurm command-line contract, executing jobs on real local workers."""

import io
import fcntl
import json
import os
import re
import shlex
from pathlib import Path
import sys

from gwf.backends.local import Client, LocalStatus, get_socket_path
from gwf.executors import deserialize


work = Path(os.environ["GWF_SLURM_FIXTURE_ROOT"])
command = Path(sys.argv[0]).name
socket_path = get_socket_path(work)
# Serialize admission and its mapping publication. Only this fixture knows the
# native session-bearing IDs; the production adapter sees Slurm decimal IDs.
lock = (work / "slurm-fixture.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX)
mapping_path = work / "slurm-local-jobs.json"
mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}


def local_id(slurm_id):
    if not re.fullmatch(r"[1-9][0-9]*", slurm_id) or slurm_id not in mapping:
        raise SystemExit(f"Invalid controlled Slurm job identifier: {slurm_id}")
    return mapping[slurm_id]


if command == "sbatch":
    script = sys.stdin.read()
    target = deserialize(io.StringIO(script))
    fault_job = os.environ.get("GWFLOW_TEST_SLURM_JOB_FAULT")
    if fault_job and target.name.startswith(fault_job + "__"):
        original = shlex.split(target.spec.removeprefix("exec "))
        target.spec = "exec " + shlex.join([
            sys.executable, str(work / "job_fault.py"), str(work), *original[1:]])
    dependencies = []
    for argument in sys.argv[1:]:
        if argument.startswith("--dependency=afterok:"):
            dependencies = [local_id(item) for item in argument.split(":")[1:]]
    with Client.connect(socket_path) as client:
        execution_id = client.submit(target, deps=dependencies)
    job_id = str(10001 + len(mapping))
    mapping[job_id] = execution_id
    mapping_path.write_text(json.dumps(mapping))
    with (work / "slurm-submitted.jsonl").open("a") as stream:
        stream.write(json.dumps({"id": str(job_id), "name": target.name,
                                 "args": sys.argv[1:], "script": script}) + "\n")
    lost_ack = os.environ.get("GWFLOW_TEST_SLURM_LOSE_ACK")
    if lost_ack and target.name.startswith(lost_ack + "__"):
        raise SystemExit("Controlled lost Slurm acknowledgement")
    print(job_id)
elif command in ("squeue", "sacct"):
    selected = mapping
    if command == "sacct" and "--jobs" in sys.argv:
        requested = sys.argv[sys.argv.index("--jobs") + 1].split(",")
        selected = {job_id: local_id(job_id) for job_id in requested}
    if os.environ.get("GWFLOW_TEST_RECORD_SLURM_QUERIES"):
        with (work / "slurm-queries.jsonl").open("a") as stream:
            stream.write(json.dumps({"command": command, "args": sys.argv[1:]}) + "\n")
    if (work / "slurm-query-error").exists():
        raise SystemExit("Controlled scheduler query failure")
    if not (work / "forgotten-slurm-history").exists():
        with Client.connect(socket_path) as client:
            states = client.status()
        short = {LocalStatus.SUBMITTED: "PD", LocalStatus.RUNNING: "R"}
        for job_id, execution_id in selected.items():
            state = states.get(execution_id, LocalStatus.UNKNOWN)
            if command == "squeue" and state in short:
                print(f"{job_id};{short[state]}")
            elif command == "sacct":
                print(f"{job_id}|{'PENDING' if state == LocalStatus.SUBMITTED else 'TIMEOUT' if state == LocalStatus.KILLED else state.name}")
elif command == "scancel":
    local_id(sys.argv[-1])
    # A cancellation request is deliberately not a confirmed cancellation.
    with (work / "slurm-cancelled.jsonl").open("a") as stream:
        stream.write(json.dumps({"id": sys.argv[-1], "args": sys.argv[1:]}) + "\n")
    (work / "cancellation-requested").touch()
else:
    raise SystemExit(f"unexpected fixture command: {command}")
