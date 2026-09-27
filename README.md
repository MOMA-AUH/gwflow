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
backend. Use whole-workflow `gwf run`; execution selectors, groups, and
`--no-deps` are rejected. Cross-task completion ordering and invalidation
propagation, retry-race handling, and collapsed status reporting remain in
Issues #7, #8, and #10 respectively. Until status projection is implemented,
`gwf status` reports ordinary inner targets and may report deleted
intermediates as needing work even when `gwf run` can reuse their task.
