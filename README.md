# gwflow

gwflow adds managed Tasks to [gwf](https://gwf.app/): computation runs in disposable
work storage, while named retained files are copied into stable results storage.
Checked completion evidence lets a Task remain reusable after its work is removed.

The v0.4.1 release removes the ignored `Task(working_dir=...)` option and requires
the current inode-only storage identity records for managed operations. See the
compatibility and transition guidance below before upgrading existing runs.

The v0.4.0 release adds per-target Apptainer images, declared-input staging with
custom names, and image-aware Reuse to the managed lifecycle. Tasks can combine
host and container commands while keeping named dependencies, partial retries,
fresh attempts, separate work/results filesystems, interrupted transfer recovery,
repair, and work cleanup. The
[packaged A/B/C example](examples/packaged/README.md) demonstrates completing two
producers, cleaning their work, then computing a new consumer from retained
results with host or container execution. The
[v0.4.0 validation record](docs/validation-v0.4.0.md) maps that release's acceptance
matrix to tests and infrastructure observations, including the BeeGFS storage
correction; it is historical evidence, not a guarantee of current compatibility.

Before v1.0, each release supports one authoring interface and one durable record
format: the ones documented by that release. Factories must use the managed API.
There is no cross-version Reuse, retry, or repair guarantee and no automatic
conversion, migration, or adoption of unsupported factories or records.
Initialized storage cannot be relocated. Ordinary authored top-level targets are
not supported in a managed Workflow. Genuinely incompatible future record changes
use the existing record kind/schema checks; this removal does not change them.

Finish or deliberately stop existing jobs before switching versions. Use the
matching old release for supported inspection or cleanup of old runs. When
records are incompatible, start fresh runs in separate workflow and storage
locations. Keep bookkeeping with its work and retained results; deleting
bookkeeping alone does not make existing results adoptable.

The obsolete `Task(working_dir=...)` argument is no longer accepted, including
`working_dir=None`. Remove it from Task factories: host and container computation
execute in private managed work. `Workflow(working_dir=...)` remains available for
workflow-relative path resolution; it does not select a computation working
directory. Managed temporary directories and the `managed_tmpdir` opt-out are
unchanged.

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
gwf status
gwf logs hello__write --no-pager
```

Use the normal gwf backend configuration and local workers or cluster backend.
Submission returns without waiting for computation. Run all frontend commands
for a workflow on one physical frontend; a guard serializes submission and
inspection through backend tracking persistence.

`gwf status` shows a compact colored tree. Reusable and pending Tasks stay on one
line; submitted, running, failed, canceled, and blocked Tasks expand their local
target names. Selecting a Task also expands it. For example:

```text
+ Task sample_a                reusable         work-cleaned
~ Task sample_b                running          1/3 targets completed
  |-- + align                 completed
  |-- ~ sort                  running
  `-- . index                 pending
```

Colors distinguish pending (magenta), submitted (cyan), running (blue), reusable
or completed (green), failed or canceled (red), and blocked or recovery conditions
(yellow). Symbols and labels remain readable when piped or with `gwf --no-color
status`. Counts include computation targets only. Preparation and completion jobs
appear when running or problematic, explicitly selected, or requested with
`--details`. Work-cleaned reuse, results transfer, repair-needed results, deferred
producer inputs, and blocked activity remain distinct; finishing computation alone
does not mark a Task reusable.

Use `gwf status --details` for debugging. It expands the tree and adds the current
condition, next planned action, full reason, attempt UUIDs, workspace, results and
staging paths, expected producer attempts, active consumers, and the mapping from
public Task/local target names to execution submissions, backend job IDs, and logs.

Task names and public names such as `sample_b__sort` accept glob patterns. `--group`
filters target groups (also accepting patterns). In tree view, `--status` matches
Task or target states, retaining the parent Task for context; pending, waiting,
repair and transfer recovery use `shouldrun`, reusable uses `completed`, and blocked
uses `failed`. A matching Task expands its selected children, including siblings
in other states. A failed target remains failed when a retry is planned.
`--endpoints` shows Tasks whose retained outputs have no declared consumers.

For managed workflows, `--format default` lists public computation targets,
`--format summary` counts those targets by state, and `--format grouped` counts
them by target group. In these formats, state filters apply to individual targets;
lifecycle jobs are excluded, and `--details` requires the tree format. Ordinary
gwf workflows retain their original formats and target endpoint filtering.

`gwf explain` and `gwf run --dry-run` report the same lifecycle decisions used by
run: Reuse, same-attempt retry or preparation, submission continuation, transfer,
repair, fresh computation, and deferred decisions. Reasons identify changed input
paths and metadata fields, declared structure, commands, and producer identities.
Fresh plans disclose previous-result removal. A fresh UUID is allocated only by
run; the detailed view identifies an existing attempt separately from that plan.
If any Task blocks submission, the whole-workflow preview says no jobs will be
submitted, even if other Tasks have pending work.

Use `gwf explain TASK` or `gwf status TASK` to narrow the display. These filters
still validate and plan the entire workflow. Explain accepts the same `--force`
and repeatable `--force-task` options as run; a display filter does not choose
which Tasks are forced or bypass dependencies.

Status, explain, dry-run, and cleanup preview wait for frontend bookkeeping
transitions and do not allocate attempts, reset baselines, move results, or remove
work. Scheduler and filesystem observations are **not a distributed snapshot**:
jobs and external files can change after observation, and run or cleanup rechecks
the relevant evidence before acting. Repair may defer an existing consumer until
a later invocation can compare restored metadata.

Public log names such as `hello__write` follow the selected execution generation.
Logs remain under `.gwf/logs/` after cleanup; execution-specific logs from earlier
retries and attempts also remain. Use the execution submission name from a saved
detailed view as the `gwf logs` argument to read that historical log. Human-readable
inspection output is the supported interface, not a stable machine-readable schema.

A target runs in private staging under `work/`, with a private writable `TMPDIR`
by default. `Workflow(managed_tmpdir=False)` preserves environment-selected
`TMPDIR`. Plain command strings are literal shell text; `shell()` uses named
file placeholders, quotes each substituted path as one shell argument, and uses
`{{`/`}}` for literal braces. Do not quote the placeholders yourself. Fixed-name
outputs can be declared without being bound into the command.

Targets can select a deployment-provided local Apptainer SIF explicitly:

```python
task = Task(inputs=[])
run = task.target("compute", inputs=[], outputs=["out.txt"], image="images/tool.sif")
run << "image-tool > out.txt"
task.retain("result", source=run.output("out.txt"), path="out.txt")
gwf.task_from_template("container_example", task)
```

The image must provide `/bin/bash` and every authored-command dependency; it
does not need gwflow or the host Python environment. Only the authored command
runs in Apptainer, with `/bin/bash -e` and private work as its current directory.
Preparation, output checks, transfer, and Completion run on the host. Omit
`image` to keep host execution. Image selection is per target, without Task or
Workflow defaults or image acquisition. Container targets and their Task's
preparation/completion jobs require gwf's ordinary `Bash` executor; inherited
custom executors at these boundaries are rejected before submission.

Relative images resolve from the Workflow directory, including symlinks and
paths containing spaces. The frontend records the resolved absolute path, size,
and modification time and schedules that resolved path. Every selected image
must be observable even to reuse a completed Task or repair results. Unavailable
images block the plan and preserve existing results; restoring matching metadata
resumes normal decisions. A changed image requires a fresh whole-Task attempt,
including previous-result removal under the existing ownership and activity
guards, independently of command tracking. Equivalent aliases do not invalidate
Reuse. Status, explain, and dry-run describe these decisions without launching
Apptainer or updating evidence. Run observes again.

Scheduled jobs do not recheck image identity. Deployments must keep resolved
images stable after submission: these observations are neither content hashes
nor snapshots, and changes preserving all tracked fields can go undetected.
The frontend and computation nodes must see the image, inputs, and managed
storage. The supported validation profile is Linux local workers and Slurm,
Python 3.12, gwf 2.1.1, and Apptainer 1.5.4. See the
[container validation instructions](docs/validation-v0.4.0.md) for the tested
deployment and repeatable checks. Configure the same shared storage paths on all
hosts. Local device numbers may differ, including on BeeGFS clients; gwflow does
not compare them across hosts. Directory inode numbers are used within those
trusted locations to recognize interrupted transfers and recorded work.

Container commands use `--cleanenv` and the image environment; ordinary inherited
`PYTHONPATH` is excluded while deliberate Apptainer environment overrides remain
usable. Managed TMPDIR is explicitly supplied in writable execution-private
storage alongside execution work, takes precedence over deployment TMPDIR
settings, and remains until eligible work cleanup. Concurrent executions get
separate directories even when they write the same temporary basename.

With `Workflow(managed_tmpdir=False)`, a set job-environment `TMPDIR` is explicitly
forwarded and its directory is requested writable inside the container. The
deployment supplies that directory, which may be node-local and may contain
spaces in its pathname. It is outside gwflow's cleanup ownership. If job TMPDIR
is unset, gwflow leaves image/Apptainer defaults in effect. Host targets keep
their ordinary inherited TMPDIR behavior under the same opt-out.

Ordinary system temporary directories are not redirected to managed work.
Commands that ignore TMPDIR retain deployment scratch behavior, and their
temporary files carry no managed-cleanup promise. Tools must honor TMPDIR or be
configured to use it. These settings add no independent environment Reuse key.
Missing Apptainer, unusable images,
missing image software, and nonzero exits fail through normal target logs with
target/image diagnostics and no host fallback. Files written before failure
cannot establish successful execution or Completion; zero exit still requires
the complete declared regular-file output set. Existing work cleanup and Reuse
apply to container Tasks as well.

Declared inputs of a container target are staged as symlinks in its
private work directory, using the declared input basenames. Target-output
references use their output filename; retained-output references use the
producer's retained pathname, rather than the public output name. `shell()` bindings
continue to name the original inputs and render to these staged files, with the
same quoting contract. Literal commands may use their basenames directly. The
effective layout is tracked as Task structure even when command hashes are off.
Duplicate effective paths, invalid managed relative paths, and input/output
collisions (including file/directory ancestry) are rejected before submission.

For a tool expecting `genome.fa` beside `genome.fa.fai`, declare both source files
in the Task boundary and target inputs. Both are then staged under those names;
gwflow does not discover companions or silently rename conflicting inputs.
Targets without `image=` continue reading their inputs in place.

Use `stage_as={relative_path: original_input}` to choose names or subdirectories
for already-declared target inputs. Unmapped inputs keep their default basenames.
For example, two `summary.csv` inputs can be assigned `sales/summary.csv` and
`returns/summary.csv`. An explicitly declared companion pair can share a directory:

```python
reference, index = "data/reference.fasta", "indexes/reference.index"
task = Task(inputs=[reference, index])
read = task.target("read", inputs=task.inputs, outputs=["out.txt"],
                   image="images/tool.sif",
                   stage_as={"ref/genome.fa": reference, "ref/genome.fa.fai": index})
read << shell("cat {reference} {reference}.fai > {out}",
              reference=reference, out=read.output("out.txt"))
task.retain("result", source=read.output("out.txt"), path="out.txt")
gwf.task_from_template("companions", task)
```

Bindings still name the original input; the command receives its staged path,
quoted as a shell argument. Relative nested names and spaces are supported.
Each logical input has exactly one effective staged path: overrides replace
defaults and add neither dependencies nor extra aliases. Undeclared references,
multiple assignments to one input, and collisions between defaults, overrides,
or outputs are errors. Paths follow the existing managed relative-path rules;
file/directory ancestry collisions are also rejected.

Changing the effective layout requires a fresh Task attempt under the existing
activity and ownership guards. Mapping order and an explicit override equal to
its default do not independently invalidate Reuse. Nonempty `stage_as` without
`image=` is an authoring error.

Host and container targets can be connected within one Task using the same
references. For example, with two deployment-provided images containing Bash
and the commands shown:

```python
task = Task(inputs=[])
seed = task.target("seed", inputs=[], outputs=["seed.txt"])
seed << "printf 'hello\\n' > seed.txt"
upper = task.target("upper", inputs=[seed.output("seed.txt")],
                    outputs=["upper.txt"], image="images/text.sif")
upper << shell("tr '[:lower:]' '[:upper:]' < {source} > {out}",
               source=seed.output("seed.txt"), out=upper.output("upper.txt"))
count = task.target("count", inputs=[upper.output("upper.txt")],
                    outputs=["count.txt"], image="images/count.sif")
count << shell("wc -c < {source} > {out}",
               source=upper.output("upper.txt"), out=count.output("count.txt"))
report = task.target("report", inputs=[count.output("count.txt")], outputs=["report.txt"])
report << shell("cat {source} > {out}",
                source=count.output("count.txt"), out=report.output("report.txt"))
task.retain("report", source=report.output("report.txt"), path="report.txt")
producer = gwf.task_from_template("mixed", task)
```

Another Task can declare `producer.outputs["report"]` in its boundary and target
inputs, bind that same reference in `shell()`, and select its own image. Its
staged link reads the selected producer's checked retained result, so the
producer's eligible work can be cleaned before adding the consumer. Within a
Task, staged references read checked committed upstream outputs. Internal
intermediates remain unavailable as cross-Task dependencies.

Changing any selected image refreshes the whole owning Task and its consumers,
even if the producer recreates identical retained-file metadata and command
hashes are disabled. Unrelated Tasks remain reusable. Adding or removing image
selection is also tracked; equivalent aliases preserve Reuse. Existing active
job, active consumer, uncertain admission, and ownership protections apply to
image-driven replacement, including consumers removed from the current workflow.

gwflow stages links to the checked resolved sources and requests their parent
directories read-only, plus writable private work and managed TMPDIR. Sources
behind file or parent-directory symlinks remain readable. A source directory may
contain the private work directory: its source mount stays read-only while the
explicit work and scratch mounts are writable. Tools needing writable input
data must copy it into private work themselves.

Ordinary Apptainer, site, and environment mounts remain in effect. Source-directory
mounts can expose neighboring files; accessibility does not add a declared
dependency or a Reuse key. These requested permissions do not promise individual
file isolation or audit every alternate writable alias supplied by the deployment.
Deployment owners are responsible for conflicting mount settings.

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

This Completion is checked evidence for the exact attempt and successful
executions, its input baseline and complete installed retained set. Existing
files, timestamp freshness, or a scheduler's success state alone cannot establish
it. Compared with the earlier gwf-style freshness behavior, the managed lifecycle
requires this execution and installation evidence before a Task can be reused.

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
the required ownership or execution evidence cannot establish a safe next action. Generic `gwf clean`
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

The same retry rules apply to container commands. Correcting a deployment
problem while keeping the selected image identity unchanged permits an ordinary
retry with fresh private work and TMPDIR, preserving eligible successful siblings.
Nonzero commands, unusable images, missing image software or Apptainer, and
invalid output sets establish no successful execution; there is no host fallback.
A changed selected image instead requires a fresh whole-Task attempt, including
previously successful targets, independently of command hashes.

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

Container result repair is a host-side transfer from checked work; it does not
rerun authored commands or require Apptainer in the finishing job. All selected
images must still be observable with matching identity at the frontend.
An unavailable image blocks repair or continuation while preserving existing
results; restoring matching identity resumes the usual decision. A changed image
requires fresh computation instead of repairing from older work. Previews make
the same distinction without mutations, and run observes metadata again.
Already scheduled lifecycle jobs do not recheck image identity, so deployments
must keep the selected images stable after submission.

Use `gwf clean-work` to preview completed-work cleanup. It lists recorded Tasks,
attempt UUIDs, workspace and staging locations, eligibility, and reasons. Add
`--delete` for noninteractive deletion; repeat `--task NAME` to limit the default
selection. Unknown Task names fail before any deletion.

```console
gwf clean-work
gwf clean-work --task A --task B
gwf clean-work --delete --task A --task B
```

Default cleanup removes only current completed work whose retained results still
match checked Completion and whose jobs are inactive. Failed, partial, superseded,
transferring, and repairable work is kept. Active jobs are skipped; unresolved
submissions and insufficient ownership cause requested deletion to fail. A prior
preview does not authorize a later deletion: ownership, result validity, and
activity are checked again under the same frontend guard as submission.

Cleanup removes the owned workspace and recorded transfer-staging directories,
including staging on configured storage. Retained results, external inputs, logs,
and reuse evidence remain. Active consumers reading results do not prevent
cleaning completed producer work. Empty grouping parents and unrecorded allocations
are left untouched; UUID-like directory names alone do not establish ownership.
Incidental scratch symlinks are unlinked without following their destinations.

Durable cleanup intent precedes deletion, and another `clean-work --delete` resumes
an interrupted eligible cleanup. Valid results remain reusable even if deletion
was interrupted. Once work is marked for removal, it cannot supply a later repair;
missing or changed results require fresh computation. Work cleanup therefore gives
up repair sources, while permitting new consumers to use retained results without
recreating completed producer work.

To remove work that default cleanup protects, select exact recorded attempt UUIDs
with repeatable `--attempt` options. Find the UUIDs and workspace/staging paths in
`clean-work` preview or the current attempt in `explain --details`:

```console
gwf clean-work --attempt UUID
gwf clean-work --delete --attempt UUID --attempt ANOTHER_UUID
```

This deliberately permits inactive failed, superseded, or repairable work to be
removed. Each selected attempt's preview explains the loss of successful
intermediate progress, diagnostics stored inside work, and repair sources.
Failed work cannot continue after cleanup intent is recorded; its next computation
uses a fresh attempt and repeats successful intermediate steps too. Cleaning older
attempts leaves current results reusable. Cleaning repair sources can require
fresh computation if retained results are missing or damaged; cleanup itself does
not change those results.

Do not combine `--attempt` with `--task`. Unknown selections fail before deletion.
Explicit selection never overrides ownership or inactivity checks: active, queued,
or transferring work and unresolved submissions cause a nonzero refusal. Logs,
external inputs, retained results, and required bookkeeping survive. Interrupted
explicit cleanup resumes by repeating the same `--attempt` selection with
`--delete`, even if the attempt is no longer current.

Structure is always tracked. Commands follow gwf's inherited `use_spec_hashes`
setting (default false); enable it explicitly in the workflow directory:

```sh
gwf config set use_spec_hashes true
```

Enabling command hashes is recommended; the inherited default is unchanged.
File size and modification
time checks do not detect changes preserving both, and successful execution does
not certify output contents. Resources, packages, hidden parameters, and the
software environment have no independent invalidation component. Package code,
tool versions or runtime environment changes can therefore leave a Task reusable
when its declared structure, tracked commands, selected image identity, and file
metadata stay unchanged.
Parameters matter only through those tracked effects. Use `gwf run --force-task
NAME` (or `--force` for all Tasks) when such an untracked change requires new
computation.

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
files and their grouping directories inside each Task's results directory.

The configured roots are trusted storage locations reserved for gwflow's managed
work and results. Keep them pointed at the intended shared storage: gwflow does
not authenticate the filesystem behind a path or detect replacement of a root
directory. It creates no root marker files or folders. Recovery records remain
under `.gwf`; directory checks within each operation still reject symlink
traversal and changes during rename or removal.

Durable directory identities use only inode numbers within these trusted
locations. Worker and frontend device numbers may differ on shared storage such
as BeeGFS; same-host filesystem-placement and rename/removal checks still use
device numbers. The older device-plus-inode record representation is unsupported
and cannot authorize Reuse, submission, mutation, or cleanup. This supersedes the
older-record compatibility guarantee in v0.4.0; its published validation record
describes that release, not current compatibility. Use the transition guidance
above for incompatible runs.

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
python -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
python -m unittest discover -s tests -v
python tests/release_smoke.py
```

The storage tests exercise distinct filesystems using `/dev/shm` when available
and report an explicit skip when no writable separate filesystem is available.
The timestamp tests also use controlled filesystem observations to cover lower
destination precision and unsupported timestamp preservation.
