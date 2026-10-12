# gwflow

gwflow runs workflows as reusable **Tasks**: groups of jobs with named results.
Computation uses disposable work directories; retained results stay available
after that work is cleaned. It uses [gwf](https://gwf.app/) for scheduling.

## Install

Use Linux, Python 3.12, and Bash. Installation includes gwf 2.1.1. Container
execution additionally needs Apptainer; this quickstart runs on the host.
These instructions describe the current checkout's `gwflow` CLI:

```sh
git clone https://github.com/MOMA-AUH/gwflow.git
cd gwflow
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install .
gwflow --version
mkdir demo
cd demo
```

## Run a workflow

Save this as `workflow.py` in the `demo` directory:

```python
from gwflow import Task, Workflow, task_template

gwf = Workflow()

@task_template
def greeting():
    task = Task(inputs=[])
    write = task.target("write", inputs=[], outputs=["message.txt", "scratch.txt"])
    write << "printf 'hello\\n' > message.txt; printf temporary > scratch.txt"
    task.retain("message", source=write.output("message.txt"), path="message.txt")
    return task

gwf.task(greeting(), alias="hello")
```

In the first terminal, select the local backend and enable command-change tracking:

```sh
gwflow config set backend local
gwflow config set use_spec_hashes true
```

In a **second terminal**, start workers from the same repository checkout and
leave them running:

```sh
. .venv/bin/activate
cd demo
gwflow workers -n 2
```

Back in the first terminal:

```sh
gwflow run --dry-run
gwflow run
gwflow status '*'
```

Submission returns before the jobs finish. Repeat `gwflow status '*'` until
`hello` is **reusable**, then inspect its retained result and individual job log:

```sh
cat results/hello/message.txt       # hello
gwflow logs hello__write --no-pager
```

## Everyday commands

Run without selectors to consider the whole workflow. Exact Task names and quoted
globs select whole Tasks; every selector must match:

```sh
gwflow run hello --dry-run
gwflow run 'hello*'
gwflow status                      # grouped overview
gwflow status hello                # selected Task rows
gwflow status hello --details      # include computation and lifecycle jobs
gwflow run hello --dry-run --details
```

A selected run includes its required upstream Tasks and reuses valid results.
Status focuses on the selected Tasks and explains relevant upstream blockers.
To recompute `hello`, preview and request a fresh attempt:

```sh
gwflow run hello --force --dry-run
gwflow run hello --force
gwflow status hello
```

Force applies only to directly selected Tasks; upstream dependencies undergo
normal checks. Wait for `hello` to become **reusable** again before cleaning:

```sh
gwflow clean hello                 # preview eligible disposable work
gwflow clean hello --delete
cat results/hello/message.txt      # retained result is still here
gwflow run hello                   # Reuse; no new jobs submitted
```

Cleanup preserves retained results, logs, and the records needed for Reuse.
It gives up intermediate files that could repair damaged results; if results
later become invalid, cleaned Tasks must compute again. Keep `results/` and
`.gwf/` together. Stop the local workers with Ctrl-C when finished.

Use `gwflow --help` and `gwflow COMMAND --help` for all options. Managed workflows
use `gwflow`; ordinary `gwf.Workflow` workflows continue to use `gwf`.

## More

- [Task authoring and dependencies](docs/reference.md#authoring-tasks)
- [Selection, status, logs, and backend configuration](docs/reference.md#commands-and-reports)
- [Container images and staged inputs](docs/reference.md#containers)
- [Lifecycle, recovery, cleanup, and storage](docs/reference.md#lifecycle-and-recovery)
- [Packaged producer/consumer example](examples/packaged/README.md)
