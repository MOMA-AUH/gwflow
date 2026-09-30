# gwflow

gwflow adds managed Tasks to [gwf](https://gwf.app/): computation runs in disposable
work storage, while named retained files are copied into stable results storage.
Checked completion evidence lets a Task remain reusable after its work is removed.

The development branch is migrating to v0.3.0. The current managed lifecycle
supports Task graphs, named Task dependencies, external inputs, and partial
retries, fresh attempts, separate work/results filesystems, and interrupted
transfer recovery. Repair and managed cleanup are being added in
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

Connect Tasks through the retained names returned by registration:

```python
consumer = Task(inputs=[hello.outputs["message"]])
read_message = consumer.target("read", inputs=consumer.inputs, outputs=["copy.txt"])
read_message << shell("cat {source} > {out}", source=hello.outputs["message"], out=read_message.output("copy.txt"))
consumer.retain("copy", source=read_message.output("copy.txt"), path="copy.txt")
gwf.task_from_template("consumer", consumer)
```

The consumer does not need the producer's filename or results root. Handles
expose retained public names only and belong to the Workflow that registered
them. Cross-Workflow handles, internal outputs from another Task, undeclared
bindings, and Task dependency cycles fail before submission.

Consumer attempts record their expected producer attempt identities before any
input is consumed. Preparation waits for checked Completion of those exact
attempts, including producer branches with no retained output; computation and
finishing recheck the association and input metadata. Existing files alone do
not satisfy this dependency. Independent Tasks remain concurrent, and submission
returns while producer work is still queued or running.

Adding a consumer after completed producers' disposable work has been removed
computes only the consumer. Their retained files and required bookkeeping must
remain intact. Detailed status and explain show expected producers and active
consumers, including queued preparation and finishing jobs. Persisted consumer
bindings remain visible even if that consumer is later omitted from the loaded
workflow; removing a Python declaration does not end its admitted jobs.

A scheduled preparation job precedes computation and records each input alias,
resolved destination, byte size, and nanosecond mtime. It observes changes made
before it runs. Its first committed baseline is immutable, including across
preparation retries. An interrupted preparation can restart when all admitted
jobs are confirmed inactive and computation has not started. Changed inputs
prevent continuation and results installation under the old attempt; an ordinary
run starts fresh work once conflicting activity has settled and inputs are valid. Neither input
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

An ordinary `gwf run` recovers interrupted finishing under the same Task attempt
without repeating verified computation. Partial copying restarts as a complete
copy from checked work; a fully prepared set can be installed directly. After
installation, recovery checks the exact operation, directory ownership, complete
file set, and separately recorded destination metadata before recording
Completion. With this evidence intact, it can finish even after work removal.
Consumers still wait for final Completion.

When installed association or copied metadata is insufficient, recovery rebuilds
the whole set from valid work only after checking inactivity and consumer
protection. Missing ownership evidence blocks removal. Invalid work requires
another computation execution: an eligible partial attempt retries its targets,
or a fresh attempt starts when the previous finishing state cannot be continued.
Changed tracked inputs, commands, or structure also follow the fresh-attempt
rules. `status`, `explain`, and dry-run identify transfer recovery without
restarting copying or changing records. Unknown submissions remain protected.

Results and bookkeeping must survive work removal. An unchanged completed Task
can be reused without its work. A subsequent fresh attempt recreates an absent
work root with new recorded ownership before removing any previous results.
Interrupted recreation can resume; an unexplained replacement root is rejected.
Incomplete attempts, damaged results, and uncertain submissions fail explicitly when
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

An ordinary run starts a fresh attempt when declared structure, tracked commands,
input metadata, or a bound producer attempt changes. Declaration order alone does
not count as a change. Refresh selected exact Task names, or every Task, with:

```sh
gwf explain --force-task hello
gwf run --force-task hello --dry-run
gwf run --force-task hello
gwf run --force
```

`--force-task` is repeatable and still plans the whole workflow. Refreshing a
producer also refreshes its consumers, even if regenerated files have identical
size and mtime; unrelated reusable Tasks stay unchanged. Do not combine it with
`--force`. Inner-target selectors, groups, and dependency bypass are unsupported.

Starting a fresh attempt removes that Task's previous results before submitting
jobs. Explain and dry-run disclose this removal without changing files. Run
validates the whole workflow, ownership, and conflicting activity first. Active
Tasks, active consumers, and unresolved admissions block conflicting replacement;
force does not cancel or duplicate their jobs. Old disposable work remains owned
by its old attempt for later cleanup.

Initialization records the selected attempt and previous result ownership before
removal, then records readiness before admission. An ordinary invocation resumes
an interrupted initialization under that same identity. A changed pending
definition requires new initialization. Failure after removal leaves the previous
results absent; there is no rollback. Invalid ownership evidence or substituted
managed links block removal.

When retained files are missing or their size or modification time changes,
ordinary run repairs the complete result set from verified committed work. Repair
preserves the Task attempt, uses a new transfer operation, and may overwrite manual
edits. The old Completion becomes ineligible before repair is admitted. The whole
replacement is copied privately before installation intent is recorded and the
owned damaged set is removed. Interrupted copying, removal, installation, and
Completion recording resume under that operation. Active or uncertain producer
work and consumers block repair, including consumers omitted from the loaded
workflow.

If the required work or its success evidence is missing or invalid, run starts a
fresh attempt and recomputes. The command tracking setting also governs repair
eligibility. Consumers with an existing input baseline defer comparison during
same-attempt repair; explain reports when a later ordinary invocation must replan.
Restored metadata that still matches permits consumer reuse. Different destination
timestamp precision can instead require new consumer computation, even though the
producer attempt stays the same. Missing results or staging roots are recreated
only after guarded ownership and inactivity checks.

Structure is always tracked. Commands follow gwf's inherited `use_spec_hashes`
setting (default false); enabling it is recommended. File size and modification
time checks do not detect changes preserving both, and successful execution does
not certify output contents. Resources, packages, hidden parameters, and the
software environment have no independent invalidation component.

Storage placement belongs to the pipeline; the same Task factory and named
output references work with different initial roots:

```python
gwf = Workflow(
    work_root="/scratch/project/work",
    results_root="/durable/project/results",
    results_staging_root="/durable/project/staging",
)
handle = gwf.task_from_template("sample_a", task, result_dir="samples/a/report")
```

Roots default to `work/` and `results/` beneath the workflow directory. A Task's
results directory defaults to its name; `result_dir` supplies an optional relative
grouping path, and the factory's retained mappings supply filenames inside it.
Task directories can share grouping parents but cannot contain one another.

The default transfer staging is `.gwf/gwflow/staging/` when bookkeeping and
results share a filesystem. Otherwise configure staging explicitly on the
results filesystem, outside results and work. Invalid placement fails before
initialization. Retained files are copied across filesystems, then the complete
staged directory is renamed into results on the same filesystem. Source work
files remain intact. Transfer preserves modification times where supported and
records actual destination metadata separately, including filesystem precision
differences or unsupported timestamp preservation. Results contain only retained
files and their grouping directories.

Equivalent resolved root spellings and root aliases are accepted. After initial
use, changing recorded roots or an existing Task's `result_dir` is rejected;
storage is not relocated or adopted. Newly added Tasks can choose new valid
directories under the recorded roots.

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

The storage tests exercise distinct filesystems using `/dev/shm` when available
and report an explicit skip when no writable separate filesystem is available.
The timestamp tests also use controlled filesystem observations to cover lower
destination precision and unsupported timestamp preservation.
