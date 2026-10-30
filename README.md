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
gwflow logs hello                   # list current jobs and available logs
gwflow logs hello__write --no-pager
```

## Everyday commands

Run without selectors to consider the whole workflow. Exact task names and quoted
globs select whole tasks; every selector must match:

```sh
gwflow run hello --dry-run
gwflow run 'hello*'
gwflow status                      # interactive view in a capable terminal
gwflow status --static             # print one report
gwflow status hello                # inspect one task
gwflow wait hello                  # wait for existing work to complete
gwflow logs hello --history        # include earlier retries and attempts
```

A selected run includes its required upstream tasks and reuses valid results.
The preview shows the planned jobs. Submission does not mean computation has finished.
Use status to follow progress. Redirected reports are plain text.

`gwflow wait` observes existing Task attempts without submitting, retrying, recovering,
or canceling work. Exact names and quoted patterns select Tasks and their required
upstream Tasks. No selectors means all declared Tasks. Downstream and unrelated
Tasks stay outside the selected scope.

Waiting fixes the Task set and attempt identities at startup. Each inspection
loads the current workflow and inputs. A replaced attempt, removed Task, or tracked
change that invalidates the awaited work ends the wait. Newly declared Tasks do
not expand the scope. Success requires valid retained results and successful
completion jobs, including record compaction.

The first inspection starts immediately. Later inspections pause for 60 seconds.
Use `gwflow wait hello --interval 5` to change the pause to five seconds. Waiting
has no time limit by default. Scheduler-query failures and unknown job outcomes keep waiting
until sufficient observations or checked execution evidence resolve them. Known
failures and invalidated attempts still end the wait promptly. Other loading or
inspection errors also end the wait.

Use `gwflow wait hello --timeout 300` to limit the whole command to five minutes.
The limit starts at command startup. It includes configuration and workflow loading,
lock waits, scheduler queries, other inspection work, and pauses. Scheduler failures
and unknown job outcomes do not reset the limit. Pauses stop when the remaining
time expires. The timeout must be a finite number greater than zero.

In a terminal, waiting updates a compact table with Task states and job progress.
Redirected output reports state changes and a final result. Routine inspection
phases and internal submission names stay hidden. Failures, required actions,
scheduler problems, and recovery retain their explanations.
Unchanged inspections stay silent. All messages, including output from workflow
loading, go to stderr. Stdout stays empty. Use `gwflow wait hello 2>wait.log` to
capture the messages. Exit codes are `0` for completion, `1` for failed work or
another waiting error, `2` for invalid command usage, `124` for timeout, and `130`
for Ctrl+C.

If completion needs another command, waiting returns `1`. This includes Tasks
without attempts, deferred work, and work that needs retry or repair. Timeout and
Ctrl+C stop waiting and clean up its inspection processes, temporary resources,
and locks. Timeout does not establish Task failure. Submitted jobs can remain queued or running
after any unsuccessful wait. A nonzero exit does not establish that jobs stopped.

To stop a task's jobs, preview with `gwflow cancel hello --dry-run`, then request
with `gwflow cancel hello`. Wait for the jobs to stop before retrying with
`gwflow run hello`. Cancellation preserves completed work and retained results.

Command changes trigger a fresh task attempt automatically, even after cleanup.
For package or environment changes that leave commands and inputs unchanged,
request a fresh attempt with `--force`:

```sh
gwflow run hello --force --dry-run
gwflow run hello --force
gwflow status hello
```

Force applies only to directly selected tasks. Upstream dependencies undergo
normal checks. Wait for `hello` to become `reusable` again before cleaning:

```sh
gwflow clean hello                 # preview eligible disposable work
gwflow clean hello --delete
cat results/hello/message.txt      # retained result is still here
gwflow run hello                   # Reuse; no new jobs submitted
```

Cleanup preserves retained results, logs, and the records needed for reuse.
If retained results later become invalid, cleaned tasks must compute again.
Keep `results/` and `.gwf/` together. Stop the local workers with Ctrl-C when finished.

Use `gwflow --help` and `gwflow COMMAND --help` for all options. Managed workflows
use `gwflow`; ordinary `gwf.Workflow` workflows continue to use `gwf`.

## More

- [Operating, upgrading, and recovering workflows](docs/operating.md)
- [Task authoring and dependencies](docs/reference.md#authoring-tasks)
- [Selection, status, logs, and backend configuration](docs/reference.md#commands-and-reports)
- [Container images and staged inputs](docs/reference.md#containers)
- [Lifecycle, recovery, cleanup, and storage](docs/reference.md#lifecycle-and-recovery)
- [Packaged producer/consumer example](examples/packaged/README.md)
