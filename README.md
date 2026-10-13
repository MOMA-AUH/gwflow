# gwflow

[![Conda Version](https://img.shields.io/conda/vn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow) [![Conda Downloads](https://img.shields.io/conda/dn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow)

gwflow runs workflows as reusable tasks: groups of jobs with named results.
Computation uses disposable work directories; retained results stay available
after that work is cleaned. It uses [gwf](https://gwf.app/) for scheduling.

## Install

Use Linux and Bash. Install the released package from the
[MOMA-AUH](https://anaconda.org/MOMA-AUH/gwflow) conda channel and gwf 2.1.1.
Container execution additionally needs Apptainer; this quickstart
runs on the host.

```sh
conda create -n gwflow --override-channels -c MOMA-AUH -c gwforg -c conda-forge gwflow
conda activate gwflow
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

In a second terminal, activate the environment and start workers in the same
`demo` directory. Leave them running:

```sh
conda activate gwflow
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
`hello` is `reusable`, then inspect its retained result and individual job log:

```sh
cat results/hello/message.txt       # hello
gwflow logs hello                  # current jobs and log-reading commands
gwflow logs hello__write --no-pager
```

## Everyday commands

Run without selectors to consider the whole workflow. Exact task names and quoted
globs select whole tasks; every selector must match:

```sh
gwflow run hello --dry-run
gwflow run 'hello*'
gwflow status                      # grouped overview
gwflow status hello                # selected task rows
gwflow status hello --details      # include computation and lifecycle jobs
gwflow run hello --dry-run --details
```

A selected run includes its required upstream tasks and reuses valid results.
Status focuses on the selected tasks and explains relevant upstream blockers.
Use `gwflow logs` for all current task log indexes, or `gwflow logs 'hello*'`
for a selected group. Each index shows failures first and links to individual
streams and the recovery preview. Add `--stderr` only when reading one job's log.
To recompute `hello`, preview and request a fresh attempt:

```sh
gwflow run hello --force --dry-run
gwflow run hello --force
gwflow status hello
```

Force applies only to directly selected tasks; upstream dependencies undergo
normal checks. Wait for `hello` to become **reusable** again before cleaning:

```sh
gwflow clean hello                 # preview eligible disposable work
gwflow clean hello --delete
cat results/hello/message.txt      # retained result is still here
gwflow run hello                   # Reuse; no new jobs submitted
```

Cleanup preserves retained results, logs, and the records needed for Reuse.
It gives up intermediate files that could repair damaged results; if results
later become invalid, cleaned tasks must compute again. Keep `results/` and
`.gwf/` together. Stop the local workers with Ctrl-C when finished.

Use `gwflow --help` and `gwflow COMMAND --help` for all options. Managed workflows
use `gwflow`; ordinary `gwf.Workflow` workflows continue to use `gwf`.

## More

- [Task authoring and dependencies](docs/reference.md#authoring-tasks)
- [Selection, status, logs, and backend configuration](docs/reference.md#commands-and-reports)
- [Container images and staged inputs](docs/reference.md#containers)
- [Lifecycle, recovery, cleanup, and storage](docs/reference.md#lifecycle-and-recovery)
- [Packaged producer/consumer example](examples/packaged/README.md)
