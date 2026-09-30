# gwflow

gwflow adds managed Tasks to [gwf](https://gwf.app/): computation runs in disposable
work storage, while named retained files are copied into stable results storage.
Checked completion evidence lets a Task remain reusable after its work is removed.

The development branch is migrating to v0.3.0. The current managed lifecycle
supports Task graphs, external inputs, and partial retries on one filesystem.
Cross-Task dependencies, repair, force, and managed cleanup are being added in
[the implementation queue](https://github.com/MOMA-AUH/gwflow/issues/62).
Existing v0.2 factories and records are not converted or adopted. The older
examples will be migrated with the complete workflow demonstration.

Install with Python 3.12 and gwf 2.1.1:

```sh
python -m pip install .
```

Create `workflow.py`:

```python
from gwflow import Task, Workflow, shell

gwf = Workflow()
task = Task(inputs=[])
write = task.target("write", inputs=[], outputs=["message.txt", "scratch.txt"])
write << shell(
    "printf 'hello\\n' > {message}; printf temporary > scratch.txt",
    message=write.output("message.txt"),
)
task.retain("message", source=write.output("message.txt"), path="message.txt")
hello = gwf.task_from_template("hello", task)
# hello.outputs["message"] is the named retained-output reference.
```

Run and inspect through gwf:

```sh
gwf explain --details
gwf run --dry-run
gwf run
gwf status --details
gwf logs hello__write --no-pager
```

Use the normal gwf backend configuration and local workers or cluster backend.
Submission returns without waiting for computation. Run all frontend commands
for a workflow on one physical frontend; a guard serializes submission and
inspection through backend tracking persistence.

A target runs in private staging under `work/`, with a private writable `TMPDIR`
by default. `Workflow(managed_tmpdir=False)` preserves environment-selected
`TMPDIR`. Plain command strings are literal shell text; `shell()` uses named
file placeholders, quotes each substituted path as one shell argument, and uses
`{{`/`}}` for literal braces. Do not quote the placeholders yourself. Fixed-name
outputs can be declared without being bound into the command.

Declare external files in both the Task boundary and each target that reads them:

```python
task = Task(inputs=["input.txt"])
read = task.target("read", inputs=["input.txt"], outputs=["copy.txt"])
read << shell("cat {source} > {copy}", source="input.txt", copy=read.output("copy.txt"))
task.retain("copy", source=read.output("copy.txt"), path="copy.txt")
gwf.task_from_template("copy", task)
```

Relative input paths are Workflow-relative. Commands receive absolute declared
paths, including symlink aliases; files are read in place without automatic
copying or linking into work. Inputs must resolve to regular files outside this
Workflow's managed storage. Boundary declarations and target inputs are required
even when a command binds a file.

A scheduled preparation job precedes computation and records each input alias,
resolved destination, byte size, and nanosecond mtime. It observes changes made
before it runs. Its first committed baseline is immutable, including across
preparation retries. An interrupted preparation can restart when all admitted
jobs are confirmed inactive and computation has not started. Changed inputs
block continuation and results installation with a fresh-attempt diagnostic;
automatic fresh-attempt replacement is a later lifecycle slice. Neither input
locking nor snapshots nor an atomic observation of multiple files is promised.
`preparation_defaults` and `completion_defaults` overlay Workflow defaults for
the respective scheduled jobs, independently of Task computation defaults.
Inspect preparation logs with `gwf logs copy__gwflow_prepare --no-pager`.

A zero command exit and every declared output being a regular file are required
before the output set commits. Finishing copies retained files into private
staging and installs the complete set in `results/hello/` before recording
Completion. Retained files are independent copies; only declared retained files
appear in results. Scratch, work outputs, and execution IDs remain in work or
bookkeeping. A Task may retain no files but still requires checked computation.

Results and bookkeeping must survive work removal. An unchanged completed Task
can be reused without its work. Incomplete attempts, damaged results, changed
tracked computation, and uncertain submissions currently fail explicitly when
they require a recovery operation that is not yet available. Generic `gwf clean`
and `gwf touch` are rejected for managed workflows. Ordinary `gwf.Workflow`
commands keep their usual behavior.

Interrupted submissions are reconciled by ordinary `gwf run`. Each backend
admission has a unique job name and durable intent identifying its backend,
operation, and dependency generations. Acknowledged IDs, saved gwf tracking,
and matching execution evidence let submission continue without repeating
accepted work. The pinned local and Slurm backends can also recover activity
from an acknowledged ID when frontend tracking was interrupted.

A failure known to precede the backend call permits another admission. An
arbitrary backend exception, missing ID, or `UNKNOWN` scheduler state does not
prove rejection or inactivity. Unresolved submissions block run with the Task
and submission name; no uncertainty override is provided. Status and explain
show the same decision without modifying managed records. Later matching
execution evidence can resolve uncertainty, and expired scheduler history does
not invalidate checked Completion. Retries require confirmed inactivity for every execution being replaced.

Within a Task, declare upstream outputs as target inputs and bind those same
references into commands:

```python
count = task.target("count", inputs=[read.output("copy.txt")], outputs=["count.txt"])
count << shell("wc -l < {source} > {out}", source=read.output("copy.txt"), out=count.output("count.txt"))
```

Add targets before registering the Task. References resolve to checked committed
output sets; target-local filenames can repeat in different targets. Foreign
references, undeclared inputs, and cycles fail before submission. Finishing
waits for every branch, including targets whose outputs are not retained.

An ordinary run retries failed or interrupted targets in the same eligible
attempt, with new execution directories and private temporary storage. Valid
successful siblings are preserved. Replacing an upstream execution also
replaces its dependents, even if regenerated files have matching metadata.
An unrelated running sibling can continue. Queued or running computation
dependents block replacement until they settle; run does not cancel them.
An already queued finishing job keeps its original bindings. Retry can proceed
while that job remains queued, but finishing is deferred until a later run after
the obsolete submission settles. If the scheduler leaves failed dependencies
queued, cancel those jobs using the backend's normal mechanism and wait for
confirmed inactivity before running again.

Retries require the original input baseline and workspace. Resource changes
permit continuation. Structural changes and enabled command changes require a
fresh attempt. With command tracking disabled, retry commands may change while
successful siblings retain earlier outputs; enabling tracking later requires
valid baselines for every selected execution. Interrupted output sets without
success evidence are rerun, and abandoned work remains owned for later cleanup.
Status, explain, and dry-run show planned retries without allocating executions.

Structure is always tracked. Commands follow gwf's inherited `use_spec_hashes`
setting (default false); enabling it is recommended. File size and modification
time checks do not detect changes preserving both, and successful execution does
not certify output contents. Resources, packages, hidden parameters, and the
software environment have no independent invalidation component.

Work, results, and bookkeeping roots must be disjoint. Configured storage must
be visible to jobs and support coherent record visibility and atomic directory
rename on the relevant filesystem. Existing unowned destinations, changed
recorded locations, malformed records, and symlink traversal are rejected.
Records are atomically published with file and directory synchronization where
supported; these guarantees are not universal power-loss certification.

Run the installed-package tests with:

```sh
python -m unittest discover -s tests -v
python tests/release_smoke.py
```
