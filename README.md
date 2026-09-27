# gwflow

gwflow groups ordinary gwf targets into named Tasks. This first slice runs all
targets through gwf's existing CLI. Task reuse after intermediate cleanup is
planned for later issues.

Install with Python 3.12 and gwf 2.1.1 from the `gwforg` Conda channel:

```bash
conda create -n gwflow python=3.12 pip gwf=2.1.1 -c gwforg -c conda-forge
conda activate gwflow
python -m pip install .
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

Start local workers and run the whole workflow using gwf:

```bash
gwf -b local workers -n 2
# In another terminal, from the workflow directory:
gwf -b local run
```

The targets appear as `alpha__copy`, `alpha__finish`, `beta__copy`, and
`beta__finish` in gwf. Each registration snapshots the Task definition.
File paths remain exactly as authored, so instances must choose distinct
output paths. Unless specified on the Task, target working directories
inherit the containing Workflow's directory.
