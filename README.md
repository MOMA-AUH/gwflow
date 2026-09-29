# gwflow

[![Conda Version](https://img.shields.io/conda/vn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow) [![Conda Downloads](https://img.shields.io/conda/dn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow)

**Reuse finished parts of a gwf workflow, even after cleaning up their
temporary files.**

In a large workflow, one useful result may take several targets and many
intermediate files to produce. You often want to keep the result and delete the
intermediates to save space. In an ordinary gwf workflow, removing those files
can make the targets look unfinished on the next run. gwflow lets you group
those targets into a **Task** with declared external inputs and retained outputs
(the files you keep). Once the task finishes, later runs can reuse it without
recreating its deleted intermediates.

For example, a mapping task could keep an alignment file while discarding
temporary chunks. A downstream task can use that alignment file; it waits for
the whole mapping task to finish. Independent tasks can still run at the same
time.

gwflow is for people who already use gwf and want to clean up intermediates or
share reusable task definitions across pipelines. You still run and inspect the
workflow with gwf's CLI.

## Install

For a published release, create an environment with Python 3.12:

```bash
conda create -n gwflow python=3.12 gwflow=0.1.0 -c MOMA-AUH -c gwforg -c conda-forge
conda activate gwflow
```

To use this checkout instead, create its development environment and install the
package:

```bash
conda env create -f environment.yml -p ./.conda-env
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate ./.conda-env
python -m pip install --no-deps --no-build-isolation -e .
```

The verified setup uses gwf 2.1.1 and its local backend.

## Try a Task

The example below is also in [`examples/uppercase`](examples/uppercase). Make a
new directory with an input file, then add the two Python files below:

```bash
mkdir gwflow-demo && cd gwflow-demo
printf 'hello gwflow\n' > input.txt
```

`task_library.py` defines the work. The `.tmp` file is internal; the `.txt` file
is the retained output:

```python
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

`workflow.py` gives two instances of that task distinct names and output paths:

```python
from gwflow import Workflow
from task_library import uppercase

gwf = Workflow()
gwf.task_from_template("alpha", uppercase("alpha"))
gwf.task_from_template("beta", uppercase("beta"))
```

From the directory containing those files, start local workers in one terminal:

```bash
gwf -b local workers -n 2
```

In another terminal with the same environment active, run the workflow:

```bash
gwf -b local run
```

When the jobs finish, `alpha.txt` and `beta.txt` contain the uppercase text. You
can check progress with `gwf -b local status`. Each task also has a completion
entry such as `alpha__gwflow_complete`.

Now remove the two internal files and run again:

```bash
rm alpha.tmp beta.tmp
gwf -b local run
```

The finished tasks are reused: gwf submits no new jobs, and the `.tmp` files
stay absent. Keep the `.gwf/` directory beside the outputs; it contains the
completion records needed for reuse.

Generated Completion jobs inherit the workflow's `defaults`. Use
`completion_defaults` to give these bookkeeping jobs smaller resources while
retaining any site options shared with ordinary targets:

```python
gwf = Workflow(
    defaults={"account": "my-account", "queue": "my-partition", "cores": 16},
    completion_defaults={"cores": 1, "memory": "1g", "walltime": "00:05:00"},
)
```

Completion options start with workflow defaults and apply these overrides. They
do not inherit Task defaults or individual target options. Omitting
`completion_defaults` leaves the workflow defaults intact; `None` has gwf's
ordinary option handling. Resource-only changes do not invalidate completed
Tasks. Choose values accepted by your backend: a rejected Completion submission
leaves its record unpublished and can be retried with `gwf run`.

## Using gwflow in a larger workflow

- Declare every file a task reads from outside as a task input, and every result
  another task needs as a retained output. gwflow checks these boundaries before
  submission.
- Give each task instance a stable, unique name. Names qualify its targets
  (`alpha__copy`, for example); they do not change file paths. Instances
  therefore need distinct output paths.
- Put task factories in importable Python modules. Pipelines can install
  particular releases of those modules and register instances in `workflow.py`.
- Use `gwf run --dry-run` to preview whole-workflow work, `gwf run --force` to
  rerun it, and `gwf logs alpha__copy --no-pager` to inspect a target. A failed
  target can be retried with `gwf run`.

Reuse follows gwf's file modification-time rules. If you want command changes
to trigger reruns, enable gwf's command tracking (off by default):

```bash
gwf config set use_spec_hashes true
```

## Explain the next run

`gwf explain` previews an ordinary whole-workflow run using the same workflow,
backend, and command-tracking configuration as `gwf run`:

```bash
gwf explain
gwf explain --details
gwf -f workflow.py:gwf -b local explain
```

Each Task has a current condition and a separate planned action, with a concise
reason. The plan lists the targets it would submit, including generated
Completion jobs. A reusable Task needs no work even after intermediate cleanup;
a missing retained output may need only a partial rerun, and missing or invalid
Completion evidence may need only a Completion job. Ordinary targets outside
Tasks are identified separately.

Use `--details` to see every inner target and its bookkeeping Completion job,
with the current backend state and planned treatment: submission or retry,
active work left alone, up to date, or omitted by Reuse. The default reason is a
summary; details report all observable direct causes together, including missing
or unusable Completion evidence, changed declarations or target membership,
missing or stale boundary files, configured command checks, and backend states
that prevent Reuse. Relevant names and paths accompany the evidence.

Command checks follow `use_spec_hashes`; equal file mtimes remain up to date,
and an `UNKNOWN` backend state alone does not invalidate valid Completion
evidence. Missing files do not reveal who removed them. Unusable records may
leave prior declarations unavailable, and saved command hashes cannot recover
old command text.

Explanation submits no jobs and preserves Completion records, expected attempts,
and logs. It waits for submission bookkeeping on the same frontend to finish,
without waiting for compute jobs. Invalid workflows and backend query errors
fail before any plan is displayed. Failed or cancelled jobs can appear in a
successful explanation. This command requires `gwflow.Workflow`, including
empty workflows, and currently accepts no selectors or force option.

The plan reflects current observations and agrees with run while definitions,
files, configuration, and backend state remain unchanged. It does not guarantee
future scheduler admission or strengthen gwf's Completion guarantees. Output is
human-readable text, not a stable machine-readable format.

Use whole-workflow `gwf run`: target selectors, `--group`, and `--no-deps` are
unsupported.
Submit a given workflow from one physical frontend node. Concurrent `gwf run`
commands for that workflow are serialized on that node; compute jobs may run
on other nodes.

For an example with installable task packages and a three-task dependency graph,
see the [packaged workflow](examples/packaged/README.md).
