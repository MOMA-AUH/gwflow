"""Controlled Slurm command-line contract, executing jobs on real local workers."""

import io
import json
import os
from pathlib import Path
import sys

from gwf.backends.local import Client, LocalStatus
from gwf.executors import deserialize


work = Path(os.environ["GWF_SLURM_FIXTURE_ROOT"])
command = Path(sys.argv[0]).name
port = int(os.environ["GWF_SLURM_FIXTURE_PORT"])
if command == "sbatch":
    script = sys.stdin.read()
    target = deserialize(io.StringIO(script))
    dependencies = []
    for argument in sys.argv[1:]:
        if argument.startswith("--dependency=afterok:"):
            dependencies = [int(item) for item in argument.split(":")[1:]]
    with Client.connect(port=port) as client:
        job_id = client.submit(target, deps=dependencies)
    with (work / "slurm-submitted.jsonl").open("a") as stream:
        stream.write(json.dumps({"id": str(job_id), "name": target.name,
                                 "args": sys.argv[1:], "script": script}) + "\n")
    print(job_id)
elif command in ("squeue", "sacct"):
    if not (work / "forgotten-slurm-history").exists():
        with Client.connect(port=port) as client:
            states = client.status()
        short = {LocalStatus.SUBMITTED: "PD", LocalStatus.RUNNING: "R"}
        for job_id, state in states.items():
            if command == "squeue" and state in short:
                print(f"{job_id};{short[state]}")
            elif command == "sacct":
                print(f"{job_id}|{'PENDING' if state == LocalStatus.SUBMITTED else 'TIMEOUT' if state == LocalStatus.KILLED else state.name}")
elif command == "scancel":
    # A cancellation request is deliberately not a confirmed cancellation.
    (work / "cancellation-requested").touch()
else:
    raise SystemExit(f"unexpected fixture command: {command}")
