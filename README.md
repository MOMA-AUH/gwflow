# gwflow

gwflow groups ordinary gwf targets into named Tasks. A completed task can be
reused through gwf's existing CLI after its internal intermediates are removed.

Create a local development environment with Python 3.12 and gwf 2.1.1 from
the `gwforg` Conda channel, then install gwflow in editable mode:

```bash
conda env create -f environment.yml -p ./.conda-env
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate ./.conda-env
python -m pip install --no-deps --no-build-isolation -e .
```

Define a reusable task in an importable module:

```python
# task_library.py
from gwflow import Task

def uppercase(name):
    task = Task(inputs=["input.txt"], outputs=[f"{name}.txt"])
    task.target("copy", inputs=["input.txt"], outputs=[f"{name}.tmp"]) << (
        f"cat input.txt > {name}.tmp"
    )
    task.target("finish", inputs=[f"{name}.tmp"], outputs=[f"{name}.txt"]) << (
        f"tr '[:lower:]' '[:upper:]' < {name}.tmp > {name}.txt"
    )
    return task
```

Register named instances in `workflow.py`:

```python
from gwflow import Workflow
from task_library import uppercase

gwf = Workflow()
gwf.task_from_template("alpha", uppercase("alpha"))
gwf.task_from_template("beta", uppercase("beta"))
```

The files above are available in `examples/uppercase`, along with an
`input.txt`. From the same terminal used for installation, start local workers:

```bash
cd examples/uppercase
gwf -b local workers -n 2
```

From the repository root in another terminal, activate the same environment
and run the workflow:

```bash
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate ./.conda-env
cd examples/uppercase
gwf -b local run
cat alpha.txt beta.txt
```

The targets appear as `alpha__copy`, `alpha__finish`, `beta__copy`, and
`beta__finish` in gwf. Each registration snapshots the Task definition.
File paths remain exactly as authored, so instances must choose distinct
output paths. Unless specified on the Task, target working directories
inherit the containing Workflow's directory.

Before gwf builds the workflow graph, gwflow checks each task boundary. Every
input to an inner target that is not produced inside the task must appear in
the task's `inputs`. Every retained output in `outputs` must be produced by an
inner target, and every inner target must declare at least one output. A task
may consume another task's output only if the producer lists it as a retained
output. These checks use declared file paths; gwflow does not inspect shell
commands for additional file accesses. gwf continues to check for duplicate
producers, cycles, and missing input files.

Each executing task also has an ordinary `TASK__gwflow_complete` target. It
waits for all of that task's file-producing targets and atomically writes an
attempt-specific completion record under `.gwf/gwflow/`. Once this final job
has completed, you can remove internal intermediates manually. For example,
after both completion jobs in `examples/uppercase` finish:

```bash
rm alpha.tmp beta.tmp
gwf -b local run
```

An unchanged run submits no jobs and leaves those intermediates absent. Keep
`.gwf/` alongside the retained outputs: missing or invalid completion evidence
prevents reuse. Logs remain available with commands such as
`gwf logs alpha__copy --no-pager`.

Tasks consuming another task's retained outputs wait for that entire producer,
including targets that run after its retained outputs appear. Each consumer
target also depends on the producer's expected completion record. Independent
tasks can run concurrently. A reused producer supplies its existing retained
outputs and completion record without exposing deleted intermediates, so adding
new tasks does not rebuild valid existing tasks.

Before submission, gwflow uses gwf's scheduler to plan work without submitting
jobs. Upstream work expands affected downstream tasks even when their retained
outputs are currently fresh. Planning repeats until the expanded tasks and
replacement completion expectations stop changing, keeping each replacement
path stable within the invocation. Complete declarations, including whole-task
dependency cycles, are validated before any reusable targets are omitted.
`gwf run --dry-run` previews ordinary whole-workflow work, including missing or
invalid completion bookkeeping and affected downstream tasks. It does not submit
jobs, replace expected attempts, or change completion records. `gwf run --force`
bypasses task reuse and follows gwf's force scheduling for every target, even
when one is already queued or running; affected tasks receive new expected
attempts. Combine both options (`gwf run --force --dry-run`) to preview that
forced work without changing completion records or expected attempts. A preview
reflects the state observed during that invocation; backend states and files
may change before a later run, so the later submissions may differ.

Repeated runs respect queued and running targets. To retry a failed target,
run `gwf run` again; sibling targets and unrelated tasks can remain active.
Concurrent `gwf run` invocations share a short submission guard covering planning,
expected completion records, and gwf's saved command hashes and backend tracking.
The guard releases when the CLI exits, without waiting for jobs, and is released
automatically if the CLI is interrupted.

Planned inner submissions and retries replace the expected completion path
before submission. Older finalizers keep their original paths and cannot
satisfy a newer expectation. Missing completion records can be regenerated;
malformed or mismatched records get fresh paths even with command tracking
disabled. Older records are preserved because active jobs may still need them.

Planning and submission use separate backend observations. Jobs can change
state between them, so ordinary gwf observation limitations still apply.
An already queued finalizer keeps its original job dependencies: after a retry
or definition change, a later `gwf run` may be needed to recover completion.
gwflow does not cancel active jobs or rewire their dependencies. With command
tracking enabled, saved inner command hashes also prevent an old-definition
job from being hidden by a newer finalizer's record.

Reuse requires the same task boundaries, inner input/output declarations, and
target names and membership. Declaration ordering does not matter. All external
inputs must exist, and retained outputs must exist and be at least as new as
the newest input. These are gwf's modification-time rules; equal timestamps
are accepted, and file contents and sizes are not compared. Queued, running,
failed, or cancelled targets prevent reuse. Otherwise, gwf decides which
individual targets need work; an invalid task is not automatically forced.

Command-only edits follow gwf's `use_spec_hashes` setting, which is disabled by
default. Enable it with `gwf config set use_spec_hashes true` to check current
commands against the completion record and gwf's saved inner command hashes.
Package-version changes and resource-option changes add no reuse invalidators.
Completion retains gwf's usual limitations: a record does not independently
prove successful execution or correct outputs, and an unknown backend state
is allowed when the remaining evidence is valid.

This implementation is verified with Python 3.12, gwf 2.1.1, and its local
backend. Use whole-workflow `gwf run`, optionally with `--dry-run` and/or
`--force`. Individual target selectors, `--group`, and `--no-deps` are rejected
before submission.

Task libraries can be released as ordinary, separate Python or Conda packages.
A pipeline installs the exact releases it selects, for example
`mapping-tasks=1.4.2` and `somatic-tasks=2.1.0` in its Conda environment, and
imports their Python factories in `workflow.py`. These package names and
versions are illustrative. Each factory returns an independent `Task`; the
pipeline gives every instance a stable, unique name with `task_from_template`.
The task name identifies an instance, while the package version identifies an
implementation release. Changing only a package version does not invalidate
reuse. Changes to task declarations, and command changes when
`use_spec_hashes` is enabled, still follow the rules above. One environment
selects one installed version of each task package; gwflow does not provide a
task registry or separate environments per task.

`gwf status` shows one completed `TASK__gwflow_complete` entry for each
reusable task. Other tasks appear as ordinary task-prefixed targets, so queued,
running, and failed work remains visible. A reused task's deleted internal
intermediates do not appear as work to do. Status inspection leaves completion
records and expected attempts unchanged; gwf may still update its own tracking
files. The displayed entries mix tasks and targets, so summary counts are not
task counts. Additional status formats and filters are not part of gwflow's
supported inspection contract.

After a release is published, install it with
`conda create -n gwflow python=3.12 gwflow=0.1.1 -c MOMA-AUH -c gwforg -c conda-forge`.
The tag-triggered publication procedure and its required credential are in
[`docs/releasing.md`](docs/releasing.md).
