# gwflow

gwflow groups ordinary gwf targets into named Tasks. This first slice runs all
targets through gwf's existing CLI. Task reuse after intermediate cleanup is
planned for later issues.

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
