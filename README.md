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

In the first terminal, select the local backend:

```sh
gwflow config set backend local
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
gwflow status '*' --static
```

Submission returns before the jobs finish. Repeat `gwflow status '*' --static` until
`hello` is `reusable`, then inspect its retained result and individual job log:

```sh
cat results/hello/message.txt       # hello
gwflow logs hello                  # compact current jobs and stream availability
gwflow logs hello --details        # full identities, paths, and pinned commands
gwflow logs hello__write --no-pager
gwflow logs hello__write --stderr --no-pager
```

## Everyday commands

Run without selectors to consider the whole workflow. Exact task names and quoted
globs select whole tasks; every selector must match:

```sh
gwflow run hello --dry-run
gwflow run 'hello*'
gwflow status                      # interactive grouped browser on a capable terminal
gwflow status --static             # one grouped report; --plain removes decoration
gwflow status hello                # browse selected task rows
gwflow status hello --details      # include computation and lifecycle jobs
gwflow run hello --dry-run --details
```

A selected run includes its required upstream tasks and reuses valid results.
The preview states how many jobs it would submit. Actual runs show confirmed
backend acceptances and a final outcome; these counts do not mean computation
has finished. Add `run --details` for all submission receipts. Interrupted runs
keep failed or uncertain submissions visible by default.
Managed Tasks always track command changes; no opt-in is needed. Editing a
computation command requires a fresh whole-Task attempt, even after cleanup.
For changes to packages or environments that leave tracked commands and inputs
unchanged, preview and request fresh computation with `gwflow run TASK --force`.
Status focuses on the selected tasks and explains relevant upstream blockers.
In the browser, use arrows and Enter to open groups, Tasks, and jobs; Esc goes
back, `/` searches Task names, `f` filters states, and `q` quits. `r` refreshes
observations while the previous snapshot remains browsable; `?` shows all keys.
`h` reads the selected Task's saved history, and `l` chooses stdout/stderr for
the selected job or historical submission. Logs open at a bounded tail; Page
Up/Down page through content, Home/End jump to the beginning/latest, and `u`
reloads the log or history without refreshing status. Open evidence stays pinned
when `r` observes a retry or removes its Task from the selected workflow.
Uppercase `F` toggles live log following, initially off. Scrolling upward or
Home pauses it; `F` resumes. Following reads only the selected submission's log,
with visible recovery from file changes and no automatic status refresh.
Task details include full diagnostics
and buttons to copy ordinary commands for another terminal.
Use `gwflow logs` for all current task log indexes, or `gwflow logs 'hello*'`
for a selected group. Each index shows failures first, both streams' availability,
and commands for reading stdout or stderr. Add `--details` for complete attempt,
submission, and backend identities, paths, and commands pinned to exact submissions.
Public job names follow the current submission; exact submission names keep the
same log after a retry. Add `--stderr` only when reading one job's log.
Use `gwflow logs hello --history` to include earlier retries and attempts, marked
as historical; a retry can create new job submissions within the same attempt.
Combine `--history --details` to expand current and historical indexes.
Logs and cancellation also accept recorded tasks whose declarations have been
removed, marking them "no longer declared".

To stop a task's admitted jobs, preview with `gwflow cancel hello --dry-run`,
then request with `gwflow cancel hello`. Selectors are required; `cancel '*'`
explicitly addresses all tasks. Accepted requests may still be stopping, and
partial failures return an error. Completed work and retained results stay
protected. Once jobs are inactive, an ordinary `gwflow run hello` can retry or
recover eligible work; there is no pause/resume step.
Cancellation reports summarize identical outcomes under each task. Add
`--details` to the cancellation invocation for every per-job receipt and full
identities; failures and uncertain observations keep their identities by default.
Use `--plain` for undecorated output. Redirected reports are automatically plain.

If submission uncertainty keeps the Task blocked, investigate all of its
computation and lifecycle jobs first. Only after establishing that none can still
run, invoke `gwflow recover hello`. This asserts whole-Task inactivity without a
confirmation prompt and records the resolved submissions in the current attempt.
It refuses known queued or running jobs, submits nothing, and does not establish
success. Then inspect `gwflow run hello --dry-run` and use ordinary run to retry
or finish eligible work. Cancellation requests alone do not justify recovery.

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

- [Operating, upgrading, and recovering workflows](docs/operating.md)
- [Task authoring and dependencies](docs/reference.md#authoring-tasks)
- [Selection, status, logs, and backend configuration](docs/reference.md#commands-and-reports)
- [Container images and staged inputs](docs/reference.md#containers)
- [Lifecycle, recovery, cleanup, and storage](docs/reference.md#lifecycle-and-recovery)
- [Packaged producer/consumer example](examples/packaged/README.md)
