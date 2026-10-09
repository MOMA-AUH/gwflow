# gwflow

[![Conda Version](https://img.shields.io/conda/vn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow) [![Conda Downloads](https://img.shields.io/conda/dn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow)

gwflow adds managed Tasks to [gwf](https://gwf.app/): computation runs in disposable
work storage, while named retained files are copied into stable results storage.
Checked completion evidence lets a Task remain reusable after its work is removed.

The v0.5.2 release makes `gwf status --details` a compact nested view: a Target
column shows preparation, computation targets, and completion beneath each Task.
Selecting a Task, job, or group uses the same expansion. Full reasons, attempts,
paths, and backend information remain available through `gwf explain --details`.
Task states, progress, filters, and execution and recovery semantics are unchanged.

The v0.5.1 release reduces repeated planning reads for Task jobs, producer
completion evidence, shared inputs, and container images. Scoped directory
handles reduce repeated directory opens while preserving pathname validation.
These observations last for one planning pass; submission and workers still
perform fresh checks. The [planning validation record](docs/validation-planning-150.json)
documents reduced reads and opens, lifecycle regression coverage, and timing
limits: measured command times remain broadly similar. Python and Conda packages
now both require Rich `>=15.0.0,<16`.

The v0.5.0 release adds a lifecycle-aware Task overview with progress that counts
preparation, computation, and completion. Status, explain, dry-run, and run share
static terminal presentation, complete Task details, and plain output for logs.
Explain, dry-run, and run show the same intended Task plan; actual runs also
report confirmed submissions, uncertain acceptance, and jobs not attempted.
Managed status filters now select whole Tasks by their primary state, and
`--format` modes are reserved for ordinary gwf workflows. See the command guide
below for selection, presentation controls, and recovery notices.

The v0.4.2 release adds explicit `docker://` registry images, acquired on the
frontend during planning and shared across a user's workflows. Missing images
are pulled into a shared cache with progress and failure reporting; cached
images are reused without registry access. Local and acquired images use the
ordinary Input baseline for execution, recovery, and Reuse. See the
[registry example](examples/registry/workflow.py) and
[registry acceptance record](docs/validation-registry.md) for usage and local/Slurm
validation.

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
from gwflow import Task, Workflow, shell, task_template

gwf = Workflow()
@task_template
def hello_task():
    task = Task(inputs=[])
    write = task.target("write", inputs=[], outputs=["message.txt", "scratch.txt"])
    write << shell(
        "printf 'hello\\n' > {message}; printf temporary > scratch.txt",
        message=write.output("message.txt"),
    )
    task.retain("message", source=write.output("message.txt"), path="message.txt")
    return task

hello = gwf.task(hello_task(), alias="hello")
# hello.outputs["message"] is the named retained-output reference.
```

Every registered Task comes from a factory decorated with `@task_template`,
including one-off work. Register it with
`workflow.task(definition, *, key=None, alias=None, result_dir=None)`.
The prefix defaults to the factory's function name; `alias` supplies a different
prefix. An omitted key uses the prefix alone; a supplied key produces
`prefix__key`. For example, a factory named `duplex_mapping` and key `sample_A`
produce `duplex_mapping__sample_A`. Reordering registrations never changes names.
The examples below use aliases to keep their short inspection names.

Prefixes start with an ASCII letter or underscore and contain only ASCII
letters, digits, underscores, and dots. Keys allow the same characters, including
a leading digit or dot. Supplied aliases and keys must be nonempty strings;
whitespace, trailing newlines, and other characters are rejected without
normalization. Double underscores are allowed. Duplicate Task names and duplicate
public job names (`Task_name__local_target`) are authoring errors identifying both
declarations; qualification never repairs collisions. A Task name may equal a
public job name. Local target and lifecycle job names remain unchanged.

Each factory call produces an independent definition, and registration snapshots
it. Later edits cannot change registered work. Nested decorated factories use the
outer factory's identity. Current declarations retain the module-qualified factory
identity and naming components separately from persisted work and Reuse checks.
Moving a factory between modules alone preserves Reuse, including after work
cleanup. Changing its generated Task name changes identity and never adopts the
old Task's results. The previous registration method has been removed.

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

`gwf status` shows one row per Task, in dependency order, with declaration
order breaking ties. The summary classifies each displayed Task once and shows
the workflow total when filtered. Preparation, computation, and finishing phases
count as active in the summary; queued work remains separate. For example:

```text
3 Tasks shown: 1 reusable, 1 active, 1 failed
Task                              State          Jobs completed   Detail
Task sample_a                     reusable       5/5              work cleaned
Task sample_b                     running        2/5              1 job running; 2 jobs queued
Task sample_c                     failed         2/5              retry available; 1 job failed
```

Jobs completed includes preparation and completion as well as computation
steps. It counts steps, not time, effort, output correctness, or eligibility for
Reuse. Fresh attempts start at zero; retries retain valid steps and exclude
invalidated dependents. Repair reopens completion immediately, and reusable
Tasks remain fully complete after work cleanup. Deferred Tasks may retain full
prior progress while waiting for upstream result recovery. If completion cannot
be established, progress is shown as `?/N` with an explanation.

The primary state is one of pending, queued, preparing, running, finishing,
reusable, repairable, deferred, failed, canceled, or blocked. Blockage takes
precedence over failure, then cancellation, then ongoing work. Aggregate details
still show jobs running within a failed, canceled, or blocked Task. Historical
backend failures do not override valid Completion and Reuse.

Supported terminals use a static Rich presentation with a colored summary frame,
Unicode state symbols, and slim progress bars. The bars are snapshots of completed
steps; presentation adds no scheduler polling or live refresh. Narrow terminals
omit bars before losing textual states and numeric progress.

`gwf status --details` adds a Target column and nests each Task's jobs directly
below its summary row. The Task row keeps its state, progress, and brief Detail;
child rows show local target names and job states. For example:

```text
Task       Target             State       Jobs completed   Detail
sample                        running     2/5              1 job running; 2 jobs queued
           ├─ [preparation]   completed
           ├─ left            completed
           ├─ right           running
           ├─ join            queued
           └─ [completion]    queued
```

Each expansion shows `[preparation]`, every computation target in dependency
order, and `[completion]`. Only the bracketed lifecycle names are dimmed; their
states remain prominent. Plain and redirected output use ASCII tree branches.
Narrow terminals stack names and states while keeping jobs beneath their Task.

Use `gwf explain --details` for full diagnostics: the current condition, planned
action, full reason, attempt UUIDs, workspace, results and staging paths, expected
producer attempts, active consumers, public job names, available backend IDs,
and log paths. `gwf run --details` and `gwf run --dry-run --details` also include
these diagnostics.

Task names and public names such as `sample_b__sort` accept glob patterns in
status and explain. Selecting a Task or job expands the complete owning Task:
nested status rows in status, and diagnostics in explain.
`--group` selects and expands whole Tasks with any matching computation target
group. `--status` on managed status matches only the primary Task state: a blocked
Task with failed jobs matches blocked, not failed. `--endpoints` shows Tasks whose
retained outputs have no declared consumers. All counts describe complete selected
Tasks. Inspection filters preserve whole-workflow validation; managed run remains
a whole-workflow operation.

Long names and diagnostics truncate with an ellipsis in a terminal. Use
`--no-truncate` to wrap their full text. `--plain` removes colors, symbols, borders,
and graphical bars, while keeping terminal truncation independent. Redirected
stdout automatically uses plain, untruncated reports, even with forced color.
`gwf --no-color status` preserves supported layout decorations without color;
unsupported terminals fall back to plain output. Required notices always wrap
completely. These presentation controls apply consistently to expanded details
in all four command routes.

Managed workflows use this Task presentation; `--format` modes are reserved for
ordinary gwf workflows, which retain their original formats and selection rules.

`gwf explain`, `gwf run --dry-run`, and `gwf run` print the same intended
**Task / Next action / Why** plan to stdout. Actions are Run, Continue, Retry,
Finish, Repair, Reuse, Wait, Defer, or Blocked. Reasons summarize the planned
treatment; `--details` exposes changed input paths and metadata fields, commands,
producer identities, and individual planned submissions. Current job states
remain separate from next actions: a failed completion job can need Finish,
and a failed consumer can need Defer until upstream results recover.

Plans prominently disclose previous retained-result removal, including Repair
and transfer recovery that will replace existing results. Dry-run and
blocked notices use conditional wording, and required notices always wrap in
full. A fresh UUID is allocated only by run; details identify an existing
attempt separately from that plan. If any Task blocks submission, the report
says **Workflow blocked — no new jobs will be submitted** while preserving the
other Tasks' intended actions. Blocked status and explain remain successful
inspections; blocked run and dry-run return an error before submission.

Actual runs end with a submission summary, such as `Submitted 9 jobs across
3 Tasks.` Counts include preparation and completion and only new confirmed
backend acceptances from this invocation. A Task is counted when it receives
at least one new job; this does not mean the Task is fully submitted or finished.
Runs with no new submissions say so explicitly. Dry-run and globally blocked
plans do not claim submissions.

If submission stops, the report separates confirmed jobs, unknown acceptance
outcomes, and planned jobs whose backend submission was not attempted.
An exception after entering the backend may happen after acceptance; it does
not prove rejection or job execution failure. Already submitted jobs may
continue, and submission is not rolled back. `--details` includes individual
outcomes and available submission names and backend IDs. Reports and required
notices go to stdout; operational diagnostics and command errors go to stderr.
These counts use the existing admission boundary without scheduler queries.

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

Run, status, explain, and dry-run may acquire missing registry images into the
user's image cache, including images of reusable or display-filtered Tasks.
They preserve Task attempts, Input baselines, Completion records, results, and
disposable work during inspection. Cleanup does not acquire or delete images.

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
@task_template
def container_example_task():
    task = Task(inputs=[])
    run = task.target("compute", inputs=[], outputs=["out.txt"], image="images/tool.sif")
    run << "image-tool > out.txt"
    task.retain("result", source=run.output("out.txt"), path="out.txt")
    return task

gwf.task(container_example_task(), alias="container_example")
```

The image must provide `/bin/bash` and every authored-command dependency; it
does not need gwflow or the host Python environment. Only the authored command
runs in Apptainer, with `/bin/bash -e` and private work as its current directory.
Preparation, output checks, transfer, and Completion run on the host. Omit
`image` to keep host execution. Image selection is per target, without Task or
Workflow defaults. Container targets and their Task's
preparation/completion jobs require gwf's ordinary `Bash` executor; inherited
custom executors at these boundaries are rejected before submission.

For anonymously accessible registry images, use an explicit `docker://`
reference instead of a local pathname:

```python
run = task.target("compute", inputs=[], outputs=["out.txt"],
                  image="docker://ubuntu:24.04")
run << "printf 'hello from the image\\n' > out.txt"
```

Tagged and digest-pinned references are accepted, for example
`docker://registry.example/tools/demo@sha256:<64 lowercase hex digits>` with
the placeholder replaced by the source's actual digest. Bare names remain
local paths; other transports and private-registry authentication are not
provided. See [the registry example](examples/registry/workflow.py).

The frontend acquires a missing image through `apptainer pull` and publishes a
complete local SIF in `~/.cache/gwflow/images`. Set `GWFLOW_IMAGE_CACHE` to choose
another cache directory; this is the sole gwflow cache-location override.
Relative overrides resolve from the Workflow directory, and persisted image
bindings are absolute. Keep the cache outside managed work, results, and
bookkeeping storage, at the same shared path on frontend and compute nodes.
Frontend and compute-node CPU architectures must match. Workers execute the
cached SIF and never download it; there is no architecture override.

Pulls report their start and either completion or failure on stderr, including
during `gwf status`, `gwf explain`, and `gwf run --dry-run`. Failure messages include
the image reference and error. Reusing a cached image is silent.

The same explicit reference shares an entry across a user's workflows and
Task names. Different reference spellings select different entries, even if
they refer to the same registry image. A present usable entry needs neither
registry access nor Apptainer for planning; actual execution still requires
Apptainer. Tags are not polled or refreshed when their remote content moves.
Only a missing entry is pulled. A present unusable entry is reported without
replacement, and a failed pull leaves a retryable cache miss. Any failed image
dependency blocks all workflow submissions while inspection still reports
other Tasks.

Concurrent frontend processes coordinate acquisition separately for each
reference, including across workflow directories. Waiting planners reuse a
complete entry published by the owner; they cannot consume its private partial
output. Failed or interrupted owners leave a retryable miss, and later callers
can acquire it without manual lock repair. Coordination ownership is released
when the frontend exits; its external pull process does not keep that ownership.
The cache filesystem must support atomic file publication and advisory locks
between the frontend processes sharing it, including across hosts when relevant.
An interrupted process may leave private temporary files, which are never
selected as cache entries.

gwflow never evicts published images, including during work cleanup and fresh
attempts. Users control removal and must keep images stable while jobs use
them. Removal can affect several workflows; reacquisition can change local
metadata even for a digest-pinned source, requiring fresh computation under
the ordinary Input baseline rules. There is no refresh or cache-cleanup command.
User-initiated removal is not coordinated with workflows or acquisition.
Sharing a writable cache between different users is outside the supported scope.

Relative images resolve from the Workflow directory, including symlinks and
paths containing spaces. Each declared image alias is an implicit External
input of its Task, outside managed work, results, and bookkeeping storage.
Images do not become staged data inputs, command bindings, or extra source
mounts unless also explicitly declared as data inputs. Each target's image
binding is part of the declared structure: adding, removing, or changing an
alias requires a fresh whole-Task attempt, even when aliases resolve to the
same file and command tracking is disabled.

Preparation accepts the image alias, resolved absolute destination, size, and
nanosecond modification time in the ordinary immutable Input baseline. Changes
before the first baseline are accepted by preparation; retries cannot replace
an accepted baseline. Execution uses the baseline's resolved image path.
The ordinary input checks apply during computation admission, finishing,
transfer continuation, repair, and Reuse, including after work cleanup.
Retargeting an unchanged alias, size changes, and forward or backward mtime
changes require fresh computation under the existing ownership and activity
guards. Unavailable images block planning and preserve results; restoring
matching observations permits ordinary retry or repair. Status, explain, and
dry-run describe these decisions without updating Task evidence; resolving a
missing registry image may invoke Apptainer on the frontend.

Restoration and reacquisition have the same comparison rules. Restoring an image
at its previous resolved path with matching size and modification time permits
cleaned Reuse, successful-branch retry, transfer continuation, or result repair
as appropriate. A missing registry image is acquired before those decisions,
even after internal work has been cleaned. Failure leaves the old lifecycle
evidence and retained results intact and blocks every submission in the workflow.
A successful pull alone does not validate older work: even the same digest source
can produce a SIF with different local metadata and require a fresh Task attempt.

An image change refreshes the whole owning Task, including successful branches
of a partial attempt. Its consumers follow the ordinary producer-attempt rules,
even if regenerated retained files have matching metadata; independent Tasks
remain reusable. Active jobs, active consumers, and unresolved submissions keep
their existing protection against replacement. Changes after baseline publication
are checked again by workers, which never acquire a replacement image. A later
frontend invocation resolves the dependency and plans recovery or a fresh attempt.

Deployments must keep images stable while jobs use them. Metadata observations
are neither content hashes, locks, nor snapshots; changes preserving all
observed fields can remain undetected. They do not strengthen gwf's Completion
guarantees.
The frontend and computation nodes must see the image, inputs, and managed
storage. The supported validation profile is Linux local workers and Slurm,
Python 3.12, gwf 2.1.1, and Apptainer 1.5.4. See the
[container validation instructions](docs/validation-v0.4.0.md) for the tested
deployment and repeatable checks, and the
[registry acceptance record](docs/validation-registry.md) for frontend acquisition,
warm-cache execution, and worker baseline enforcement on both backends.
Configure the same shared storage paths on all
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
@task_template
def companions_task():
    task = Task(inputs=[reference, index])
    read = task.target("read", inputs=task.inputs, outputs=["out.txt"],
                       image="images/tool.sif",
                       stage_as={"ref/genome.fa": reference, "ref/genome.fa.fai": index})
    read << shell("cat {reference} {reference}.fai > {out}",
                  reference=reference, out=read.output("out.txt"))
    task.retain("result", source=read.output("out.txt"), path="out.txt")
    return task

gwf.task(companions_task(), alias="companions")
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
@task_template
def mixed_task():
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
    return task

producer = gwf.task(mixed_task(), alias="mixed")
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
selection and changes to declared aliases are also tracked. Existing active
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
@task_template
def copy_task():
    task = Task(inputs=["input.txt"])
    read = task.target("read", inputs=["input.txt"], outputs=["copy.txt"])
    read << shell("cat {source} > {copy}", source="input.txt", copy=read.output("copy.txt"))
    task.retain("copy", source=read.output("copy.txt"), path="copy.txt")
    return task

gwf.task(copy_task(), alias="copy")
```

Relative input paths are Workflow-relative. Commands receive absolute declared
paths, including symlink aliases; files are read in place without automatic
copying or linking into work. Inputs must resolve to regular files outside this
Workflow's managed storage. Boundary declarations and target inputs are required
even when a command binds a file.

Connect Tasks through the retained names returned by registration:

```python
@task_template
def consumer_task():
    consumer = Task(inputs=[hello.outputs["message"]])
    read_message = consumer.target("read", inputs=consumer.inputs, outputs=["copy.txt"])
    read_message << shell("cat {source} > {out}", source=hello.outputs["message"], out=read_message.output("copy.txt"))
    consumer.retain("copy", source=read_message.output("copy.txt"), path="copy.txt")
    return consumer

gwf.task(consumer_task(), alias="consumer")
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
images must still match the accepted Input baseline at planning and during
scheduled finishing.
An unavailable image blocks repair or continuation while preserving existing
results; restoring matching identity resumes the usual decision. A changed image
requires fresh computation instead of repairing from older work. Previews make
the same distinction without Task mutations, and run observes metadata again.
Scheduled lifecycle jobs apply ordinary input checks to images. Deployments
must still keep images stable while jobs use them; checks are not locks.

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
handle = gwf.task(hello_task(), alias="sample_a", result_dir="samples/a/report")
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
