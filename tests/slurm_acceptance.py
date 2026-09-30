"""Acceptance of an installed candidate on a real Slurm backend.

The caller installs the candidate on shared storage and runs this file with
that environment's Python. Each invocation keeps its work and evidence.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import gwf
import gwflow


GWF = str(Path(sys.executable).with_name("gwf"))
ACTIVE = {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING"}
SUCCESS = {"COMPLETED"}


class Acceptance:
    def __init__(self, root, deadline):
        self.root = root
        self.deadline = time.monotonic() + deadline
        self.commands = 0
        self.results = {}
        self.root.mkdir(parents=True, exist_ok=False)

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Acceptance deadline reached; inspect preserved Slurm jobs and logs")
        return left

    def command(self, args, *, cwd=None, timeout=90):
        self.commands += 1
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                timeout=min(timeout, self.remaining()))
        log = self.root / f"command-{self.commands:03d}.json"
        log.write_text(json.dumps({"args": args, "cwd": str(cwd), "exit": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr}, indent=2))
        if result.returncode:
            raise RuntimeError(f"{args!r} failed; see {log}: {result.stderr}")
        return result.stdout + result.stderr

    def cli(self, work, *args):
        return self.command([GWF, "-b", "slurm", *args], cwd=work)

    def tracked(self, work):
        path = work / ".gwf" / "slurm-backend-tracked.json"
        return json.loads(path.read_text())

    def states(self, ids):
        if not ids:
            return {}
        # squeue sees queued work immediately; sacct retains terminal work.
        queue = self.command(["squeue", "-h", "-j", ",".join(ids), "-o", "%i|%T"],
                             timeout=30)
        states = {}
        for line in queue.splitlines():
            job, state = line.split("|", 1)
            states[job] = state.strip().upper()
        accounting = self.command([
            "sacct", "-n", "-P", "--allocations", "-j", ",".join(ids),
            "-o", "JobIDRaw,State,ReqCPUS,ReqMem,Timelimit,Partition,Account",
        ], timeout=30)
        for line in accounting.splitlines():
            fields = line.split("|")
            if len(fields) >= 2 and fields[0] in ids and fields[0] not in states:
                states[fields[0]] = fields[1].split()[0].upper().rstrip("+")
        return states

    def wait(self, description, predicate, *, jobs=None):
        while time.monotonic() < self.deadline:
            if predicate():
                return
            time.sleep(5)
        status = self.states(list(jobs.values())) if jobs else {}
        raise TimeoutError(f"Timed out waiting for {description}; Slurm states: {status}")

    def wait_completed(self, work, names):
        jobs = self.tracked(work)
        missing = set(names) - jobs.keys()
        if missing:
            raise AssertionError(f"Ordinary or Completion jobs were not submitted: {missing}")
        ids = {name: jobs[name] for name in names}

        def done():
            states = self.states(list(ids.values()))
            bad = {name: states.get(job) for name, job in ids.items()
                   if states.get(job) and states[job] not in ACTIVE | SUCCESS}
            if bad:
                raise AssertionError(f"Slurm jobs failed: {bad}")
            return all(states.get(job) in SUCCESS for job in ids.values())

        self.wait("Slurm completion", done, jobs=ids)
        return ids

    def workdir(self, name, workflow):
        work = self.root / name
        work.mkdir()
        (work / "workflow.py").write_text(workflow)
        (work / ".gwfconf.json").write_text(json.dumps({"backend": "slurm"}))
        return work

    def record(self, label, value):
        self.results[label] = value
        (self.root / "results.json").write_text(json.dumps(self.results, indent=2, sort_keys=True))


def initial_workflow():
    return '''from gwflow import Task, Workflow
import sys

gwf = Workflow(
    defaults={"cores": 1, "memory": "2g", "walltime": "00:10:00"},
    completion_defaults={"cores": 1, "memory": "1g", "walltime": "00:05:00"},
)
python = sys.executable

alpha = Task(inputs=["alpha.in"], outputs=["alpha.out"])
alpha.target("prepare", inputs=["alpha.in"], outputs=["alpha.tmp"]) << "cp alpha.in alpha.tmp"
alpha.target("finish", inputs=["alpha.tmp"], outputs=["alpha.out"]) << (
    f"{python} probe.py alpha; cp alpha.tmp alpha.out"
)
gwf.task_from_template("alpha", alpha)

beta = Task(inputs=["beta.in"], outputs=["beta.out"])
beta.target("make", inputs=["beta.in"], outputs=["beta.out"]) << (
    f"{python} probe.py beta; cp beta.in beta.out"
)
beta.target("side", inputs=[], outputs=["beta.side.tmp"]) << (
    "touch beta.started; timeout 600 bash -c 'until test -f beta.release; do sleep 1; done' "
    "&& touch beta.side.tmp"
)
gwf.task_from_template("beta", beta)

gamma = Task(inputs=["beta.out"], outputs=["gamma.out"])
gamma.target("finish", inputs=["beta.out"], outputs=["gamma.out"]) << (
    f"{python} probe.py gamma; cp beta.out gamma.out"
)
gwf.task_from_template("gamma", gamma)
'''


def probe_source():
    return '''import json
from pathlib import Path
import shutil
import sys
import gwf
import gwflow

Path(sys.argv[1] + ".probe.json").write_text(json.dumps({
    "python": sys.executable,
    "gwf_executable": shutil.which("gwf"),
    "gwflow_package": str(Path(gwflow.__file__).resolve()),
    "gwf_package": str(Path(gwf.__file__).resolve()),
    "workflow_visible": Path("workflow.py").is_file(),
}))
'''


def require_submissions(output, names):
    for name in names:
        if f"Submitted target {name}" not in output:
            raise AssertionError(f"No submission for {name}: {output}")


def evidence_files(work):
    return {
        str(path.relative_to(work)): path.read_bytes()
        for directory in (work / ".gwf" / "gwflow", work / ".gwf" / "logs")
        if directory.exists()
        for path in directory.rglob("*") if path.is_file()
    }


def initial_scenarios(run):
    work = run.workdir("ordered", initial_workflow())
    (work / "probe.py").write_text(probe_source())
    (work / "alpha.in").write_text("alpha\n")
    (work / "beta.in").write_text("beta\n")
    names = {"alpha__prepare", "alpha__finish", "alpha__gwflow_complete",
             "beta__make", "beta__side", "beta__gwflow_complete",
             "gamma__finish", "gamma__gwflow_complete"}
    try:
        output = run.cli(work, "run")
        require_submissions(output, names)
        jobs = run.tracked(work)
        if set(jobs) != names or not all(str(job).isdigit() for job in jobs.values()):
            raise AssertionError(f"Expected actual Slurm job IDs for all targets: {jobs}")
        def independent_and_active():
            states = run.states([jobs["alpha__gwflow_complete"], jobs["beta__side"]])
            return (states.get(jobs["alpha__gwflow_complete"]) == "COMPLETED"
                    and states.get(jobs["beta__side"]) == "RUNNING"
                    and (work / "alpha.out").exists() and (work / "beta.out").exists()
                    and (work / "beta.started").exists())

        run.wait("independent alpha and active beta", independent_and_active,
                 jobs=jobs)
        if (work / "gamma.out").exists():
            raise AssertionError("Downstream Task ran before the whole beta Task finished")
        detail = run.cli(work, "explain", "--details")
        if "backend" not in detail.lower() or "beta" not in detail:
            raise AssertionError(f"Active work was not inspectable: {detail}")
        run.record("active_explanation", detail)
    finally:
        (work / "beta.release").touch()

    run.wait_completed(work, names)
    for name in ("alpha", "beta", "gamma"):
        probe = json.loads((work / f"{name}.probe.json").read_text())
        if (probe["python"] != sys.executable or probe["gwf_executable"] != GWF
                or not probe["workflow_visible"]
                or not str(Path(probe["gwflow_package"]).resolve()).startswith(str(Path(sys.prefix).resolve()))
                or not str(Path(probe["gwf_package"]).resolve()).startswith(str(Path(sys.prefix).resolve()))):
            raise AssertionError(f"Launch environment or workflow unavailable on compute node: {probe}")
    if (work / "gamma.out").read_text() != "beta\n":
        raise AssertionError("Downstream output does not match retained beta output")
    run.record("initial", {"job_ids": jobs, "node_probes": {
        name: json.loads((work / f"{name}.probe.json").read_text())
        for name in ("alpha", "beta", "gamma")}})

    # Check requested resources on the jobs Slurm actually accepted.
    accounting = run.command([
        "sacct", "-n", "-P", "--allocations", "-j", ",".join(jobs.values()),
        "-o", "JobIDRaw,ReqCPUS,ReqMem,Timelimit,Partition,Account,State",
    ])
    resources = {fields[0]: fields[1:] for fields in
                 (line.split("|") for line in accounting.splitlines()) if fields[0] in jobs.values()}
    for name, job in jobs.items():
        if job not in resources:
            raise AssertionError(f"No Slurm accounting for {name} ({job})")
        cpus, memory, limit, *_ = resources[job]
        expected_memory = "1G" if name.endswith("__gwflow_complete") else "2G"
        expected_time = "00:05:00" if name.endswith("__gwflow_complete") else "00:10:00"
        if cpus != "1" or not memory.upper().startswith(expected_memory) or limit != expected_time:
            raise AssertionError(f"Incorrect Slurm resources for {name}: {resources[job]}")
    run.record("resources", resources)

    (work / "alpha.tmp").unlink()
    (work / "beta.side.tmp").unlink()
    reused = run.cli(work, "run")
    if "Submitted target" in reused or (work / "alpha.tmp").exists() or (work / "beta.side.tmp").exists():
        raise AssertionError(f"Task was not reused after cleanup: {reused}")
    run.record("reuse_after_cleanup", "passed")

    time.sleep(1.1)
    (work / "beta.in").write_text("changed beta\n")
    changed = run.cli(work, "run")
    require_submissions(changed, {"beta__make", "beta__side", "beta__gwflow_complete",
                                  "gamma__finish", "gamma__gwflow_complete"})
    if "Submitted target alpha__" in changed:
        raise AssertionError(f"Independent Task reran: {changed}")
    run.wait_completed(work, {"beta__make", "beta__side", "beta__gwflow_complete",
                              "gamma__finish", "gamma__gwflow_complete"})
    if (work / "gamma.out").read_text() != "changed beta\n":
        raise AssertionError("Changed upstream output did not reach downstream Task")
    run.record("upstream_change", {"job_ids": run.tracked(work), "result": "passed"})

    (work / "beta.out").unlink()
    missing = run.cli(work, "run")
    require_submissions(missing, {"beta__make", "beta__gwflow_complete",
                                  "gamma__finish", "gamma__gwflow_complete"})
    if "Submitted target alpha__" in missing:
        raise AssertionError(f"Missing beta output resubmitted independent alpha: {missing}")
    run.wait_completed(work, {"beta__make", "beta__gwflow_complete",
                              "gamma__finish", "gamma__gwflow_complete"})
    run.record("missing_retained_output", {"job_ids": run.tracked(work), "result": "passed"})

    before = run.tracked(work)
    evidence = evidence_files(work)
    preview = run.cli(work, "explain", "--force")
    planned = set(re.findall(r"Would submit (\w+)", preview))
    if planned != names or run.tracked(work) != before or evidence_files(work) != evidence:
        raise AssertionError(f"Forced preview changed evidence or omitted work: {preview}")
    forced = run.cli(work, "run", "--force")
    submitted = set(re.findall(r"Submitted target (\w+)", forced))
    if submitted != planned:
        raise AssertionError(f"Forced execution differs from preview: {forced}")
    after = run.wait_completed(work, names)
    if any(before[name] == after[name] for name in names):
        raise AssertionError("Force did not submit fresh Slurm jobs")
    run.record("quiescent_force", {"job_ids": after, "preview": preview})
    return work


def inspect_coordination(run, work):
    # Hold the same frontend submission lock while real Slurm work is active.
    # Explain must report the wait, then return before the compute job finishes.
    (work / "beta.release").unlink()
    (work / "beta.side.tmp").unlink()
    (work / "beta.started").unlink()
    try:
        output = run.cli(work, "run")
        require_submissions(output, {"beta__side", "beta__gwflow_complete"})
        run.wait("active side job", lambda: (work / "beta.started").exists(),
                 jobs=run.tracked(work))
        lock_path = work / ".gwf" / "gwflow-submission.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            inspector = subprocess.Popen([GWF, "-b", "slurm", "explain", "--details"],
                                         cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True)
            try:
                time.sleep(2)
                if inspector.poll() is not None:
                    raise AssertionError("Explain did not wait behind submission bookkeeping")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        stdout, stderr = inspector.communicate(timeout=min(90, run.remaining()))
        if inspector.returncode or "Waiting for another invocation" not in stderr:
            raise AssertionError(f"Explain did not report coordination: {stdout}\n{stderr}")
        if (work / "beta.side.tmp").exists():
            raise AssertionError("Compute job finished before active inspection")
        run.record("active_inspection", {"job_ids": run.tracked(work),
                                         "stdout": stdout, "stderr": stderr})
    finally:
        (work / "beta.release").touch()
    run.wait_completed(work, set(re.findall(r"Submitted target (\w+)", output)))


def failure_workflow():
    return '''from gwflow import Task, Workflow

gwf = Workflow(defaults={"cores": 1, "memory": "1g", "walltime": "00:10:00"},
               completion_defaults={"walltime": "00:05:00"})
retry = Task(inputs=["input.txt"], outputs=["result.txt"])
retry.target("work", inputs=["input.txt"], outputs=["result.txt"]) << (
    "test -f allow-work || exit 1; cp input.txt result.txt"
)
retry.target("sibling", inputs=[], outputs=["sibling.tmp"]) << (
    "touch sibling.started; timeout 600 bash -c 'until test -f sibling.release; do sleep 1; done' "
    "&& touch sibling.tmp"
)
gwf.task_from_template("retry", retry)
downstream = Task(inputs=["result.txt"], outputs=["downstream.txt"])
downstream.target("work", inputs=["result.txt"], outputs=["downstream.txt"]) << (
    "cp result.txt downstream.txt"
)
gwf.task_from_template("downstream", downstream)
'''


def failure_recovery(run):
    work = run.workdir("recovery", failure_workflow())
    (work / "input.txt").write_text("recovered\n")
    first = run.cli(work, "run")
    require_submissions(first, {"retry__work", "retry__sibling", "retry__gwflow_complete",
                                "downstream__work", "downstream__gwflow_complete"})
    original = run.tracked(work)

    def failure_observed():
        states = run.states(list(original.values()))
        return (states.get(original["retry__work"]) == "FAILED"
                and states.get(original["retry__gwflow_complete"]) == "CANCELLED"
                and states.get(original["downstream__work"]) == "CANCELLED"
                and states.get(original["retry__sibling"]) == "RUNNING"
                and (work / "sibling.started").exists())

    try:
        run.wait("failed target, invalid-dependency cancellation, and active sibling",
                 failure_observed, jobs=original)
        if (work / "downstream.txt").exists():
            raise AssertionError("Downstream ran after upstream failure")
        run.record("failure", {"job_ids": original,
                               "states": run.states(list(original.values()))})
        (work / "allow-work").touch()
        retry = run.cli(work, "run")
        require_submissions(retry, {"retry__work"})
        if "Submitted target retry__sibling" in retry:
            raise AssertionError("Active sibling was needlessly resubmitted")
    finally:
        (work / "sibling.release").touch()

    # Slurm may cancel dependent jobs before gwf can retry them. A later
    # ordinary invocation repairs the Completion record and downstream Task.
    for attempt in range(5):
        jobs = run.tracked(work)

        def quiescent():
            states = run.states(list(jobs.values()))
            return all(states.get(job) and states[job] not in ACTIVE for job in jobs.values())

        run.wait("retry wave to settle", quiescent, jobs=jobs)
        states = run.states(list(jobs.values()))
        if ((work / "downstream.txt").exists()
                and all(states.get(job) == "COMPLETED" for job in jobs.values())):
            break
        run.cli(work, "run")
    else:
        raise AssertionError("Ordinary reruns did not recover failed Task and downstream work")
    run.wait_completed(work, {"retry__work", "retry__sibling", "retry__gwflow_complete",
                              "downstream__work", "downstream__gwflow_complete"})
    if (work / "downstream.txt").read_text() != "recovered\n":
        raise AssertionError("Recovery output is incorrect")
    run.record("failure_recovery", {"job_ids": run.tracked(work), "result": "passed"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--deadline-seconds", type=int, default=2400)
    args = parser.parse_args()
    for program in ("sbatch", "squeue", "sacct", "scontrol"):
        if not shutil.which(program):
            parser.error(f"{program} is required on the self-hosted runner")
    artifact = args.artifact.resolve()
    run = Acceptance(args.work_root.resolve(), args.deadline_seconds)
    identity = {"artifact": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "python": sys.version, "python_executable": sys.executable,
                "gwf_version": getattr(gwf, "__version__", "unknown"),
                "gwflow_package": str(Path(gwflow.__file__).resolve()),
                "runner": os.environ.get("RUNNER_NAME"), "github_sha": os.environ.get("GITHUB_SHA")}
    run.record("identity", identity)
    config = run.command(["scontrol", "show", "config"])
    relevant = [line.strip() for line in config.splitlines() if any(
        key in line for key in ("DependencyParameters", "AccountingStorageType", "JobAcctGatherType"))]
    run.record("site", {"slurm_config": relevant,
                        "partition": os.environ.get("SLURM_PARTITION", "default"),
                        "account": os.environ.get("SLURM_ACCOUNT", "default")})
    if "kill_invalid_depend" not in config.lower():
        raise AssertionError("The site must enable DependencyParameters=kill_invalid_depend")
    try:
        work = initial_scenarios(run)
        failure_recovery(run)
        inspect_coordination(run, work)
        run.record("result", "passed")
    except BaseException as exc:
        run.record("result", {"failed": repr(exc), "tracked_jobs": {
            str(path.parent.parent): json.loads(path.read_text())
            for path in run.root.glob("*/.gwf/slurm-backend-tracked.json")}})
        raise
    finally:
        for path in run.root.glob("*/.gwf/logs/*"):
            destination = run.root / "logs" / path.parent.parent.parent.name
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination / path.name)


if __name__ == "__main__":
    main()
