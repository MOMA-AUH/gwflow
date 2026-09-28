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

Use whole-workflow `gwf run`: target selectors, `--group`, and `--no-deps` are
unsupported.

For the full rules on task boundaries, completion records, retries, and
planning, see the [reuse and operations guide](docs/reuse-and-operations.md).
