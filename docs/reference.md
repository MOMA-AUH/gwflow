# Reference

Start with the [runnable quickstart](../README.md). This guide covers the current
checkout; use `gwflow COMMAND --help` for the complete option list.

- [Authoring Tasks](#authoring-tasks)
- [Commands and reports](#commands-and-reports)
- [Containers](#containers)
- [Lifecycle and recovery](#lifecycle-and-recovery)
- [Cleanup](#cleanup)
- [Change tracking](#change-tracking)
- [Storage](#storage)
- [Compatibility](#compatibility)

## Authoring Tasks

Examples below use these imports and a managed Workflow:

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
declarations; qualification never repairs collisions. A Task name must also differ
from every public computation, preparation, and completion job name, regardless of
declaration order. For example, a Task named `sample__copy` conflicts with the
`copy` target in Task `sample`; `sample__gwflow_prepare` and
`sample__gwflow_complete` conflict with that Task's lifecycle jobs. The error
identifies the conflicting name and both Tasks and factories so the relevant alias,
key, or local target name can be changed. Local target and lifecycle job names
remain unchanged.

Each factory call produces an independent definition, and registration snapshots
it. Later edits cannot change registered work. Nested decorated factories use the
outer factory's identity. Current declarations retain the module-qualified factory
identity and naming components separately from persisted work and Reuse checks.
Moving a factory between modules alone preserves Reuse, including after work
cleanup. Changing its generated Task name changes identity and never adopts the
old Task's results. The previous registration method has been removed.

A target runs in private staging under `work/`, with a private writable `TMPDIR`
by default. `Workflow(managed_tmpdir=False)` preserves environment-selected
`TMPDIR`. Plain command strings are literal shell text; `shell()` uses named
file placeholders, quotes each substituted path as one shell argument, and uses
`{{`/`}}` for literal braces. Do not quote the placeholders yourself. Fixed-name
outputs can be declared without being bound into the command.

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
remain intact. Detailed run previews show expected producers and relevant active
consumers, including queued preparation and finishing jobs. Persisted consumer
bindings still protect result replacement if that consumer is omitted from the
loaded workflow; removing a Python declaration does not end its admitted jobs.

## Commands and reports

### Executable, discovery, and backends

Use `gwflow` for `gwflow.Workflow` declarations and `gwf` for ordinary
`gwf.Workflow` declarations. The managed commands are `run`, `status`, `logs`,
`cancel`, `clean`, `config`, and `workers`.

The default workflow is `workflow.py` with an object named `gwf`. Relative file
lookup searches the current directory and its parents. Select another file or
object with a global option, for example:

```sh
gwflow --file pipelines/workflow.py:pipeline --backend local status
gwflow config set backend local
gwflow workers -n 2
```

Local workers must run in the same configured environment as the frontend.
For Slurm, use the site's configured backend and shared storage; no local worker
process is needed. `--backend` overrides configuration and backend autodetection.
The existing `.gwfconf.json`, `.gwf/`, and backend plugin discovery conventions
remain in use. Global verbosity and color options precede the command.

Run all frontend commands for a workflow on one physical frontend. A guard
serializes submission and inspection through backend tracking persistence.
Submission returns before computation finishes; repeat status until the Tasks
are reusable before depending on their final results or cleaning their work.

### Task selection and execution scope

`run`, `status`, and `clean` accept multiple exact Task names and case-sensitive
Task-name globs. Quote patterns to prevent shell expansion:

```sh
gwflow run sample_a sample_b --dry-run
gwflow run 'mapping__[AB]'
gwflow status 'mapping__*' --status failed
```

Each selector must match at least one Task. All unmatched selectors are reported,
and the entire request fails even when other selectors match. Overlapping
selectors include a Task only once. Factory groups and public job names do not
expand to Tasks; a name that is itself a declared Task name still selects it.
Run and status select from current declarations. Cleanup also selects recorded
Task names whose declarations have been removed.

No selectors means all Tasks. A selected run's Execution scope includes those
Tasks and their required upstream dependencies, but does not add downstream
consumers. In A → B → C, `gwflow run C --dry-run` shows A, B, and C;
`gwflow status C` displays C and explains relevant upstream blockers.
`gwflow clean C` inspects C's work without adding A or B.

`gwflow run C --force` requests a fresh attempt for C alone. Added dependencies
undergo normal lifecycle and Reuse checks. `gwflow run --force` selects every
declared Task. To force a producer and then continue the broader workflow, run
those as separate invocations after the producer's work settles.

The complete workflow's declarations must be valid, including dependencies and
results layout. Runtime planning and image acquisition concern only the selected
Tasks and required upstream Tasks. Unrelated runtime failures do not block a
selected run. Shared-storage ownership and active or uncertain consumer
protections still apply outside the scope, including recorded consumers removed
from current declarations.

### Status views and progress

| Request | View |
| --- | --- |
| `gwflow status` or a filter-only request | Grouped overview |
| `gwflow status '*'` or any explicit Task selectors | Flat selected Task rows |
| `gwflow status --details` with or without selectors | Selected Tasks expanded with their jobs |

Resolve Task selectors before `--status` filtering. A valid selection emptied by
the filter succeeds with `No Tasks selected.`; an unmatched selector fails.
Filtering matches the primary Task state, so a blocked Task containing failed
jobs matches `blocked`, not `failed`.

The overview groups the same qualified factory implementation and effective
name prefix. A default use and an alias matching that default share a group;
other aliases remain separate. Names are not split to infer groups. A unique
prefix displays alone; shared prefixes display `prefix@module.qualified_factory`.
These are display labels, not selectors, and are determined before filtering.
Groups rank by their earliest member in dependency order, with declaration-order
ties; filtering an early member does not reorder surviving groups.

The header reports selected Tasks against all declared Tasks. A group's Reusable
fraction counts reusable Tasks over its selected members. Overview summaries
combine preparing, running, and finishing as `active`; queued work stays separate.
For example:

```text
45 of 45 Tasks selected
33 reusable, 4 active, 6 failed, 2 queued

Group              Reusable  Other states
prepare_reference       1/1
duplex_mapping        14/24  4 active, 6 failed
mutect2_calling        18/20  2 queued
```

Task rows and expanded jobs follow stable dependency order. **Jobs completed**
counts preparation, computation, and completion steps, not elapsed time, effort,
output correctness, or eligibility for Reuse. Fresh attempts start at zero.
Retries retain valid steps and exclude invalidated dependents; Repair reopens
completion. Reusable Tasks stay fully complete after work cleanup. Deferred
Tasks may retain full prior progress while awaiting upstream result recovery.
Unknown completion appears as `?/N` with an explanation.

The primary states are pending, queued, preparing, running, finishing, reusable,
repairable, deferred, failed, canceled, and blocked. Blockage takes precedence
over failure, then cancellation, then ongoing work. Diagnostics still include
jobs running inside a failed, canceled, or blocked Task. Historical backend
failures do not override valid Completion and Reuse.

`--details` places each Task's preparation, computation, and completion jobs below
its row. Child rows show public job names and job states. It does not add attempt
paths or full planner diagnostics; use `gwflow run TASK --dry-run --details` for
those. Status is a static snapshot, without live refresh or extra scheduler
polling introduced by presentation.

### Attention, notices, and complete text

Grouped status previews up to five blocked, five failed, and five canceled Tasks
independently. Each category reports the full selected count and any omitted
count. Its job totals include observed failed, canceled, running, and queued jobs
across every category member, including omitted Tasks. Jobs removed from the
current declaration still count in activity diagnostics but not in the current
progress denominator. Incomplete observations never imply zero activity.

Each category prints a shell-quoted inspection command such as
`gwflow status '*' --status failed`. It retains relevant workflow, backend, global,
and presentation options, uses Task rows, and filters to that category. Flat and
detailed views show all selected Tasks without repeating the bounded preview.

Required actions appear in `Workflow notices`, even when a state filter leaves
no rows. Notices include all relevant blocked Tasks and reminders to run again
after upstream result recovery or after an obsolete completion job settles.
An upstream or state-filtered Task absent from the rows is marked
`(outside selection)`. These notices are uncapped within the assessed scope;
unrelated runtime blockers do not enter a selected status report. Active jobs
may continue even when no new work can be submitted. Routine work-directory
details do not create notices, and notices use the same observations as the rows.

Run and status keep all text included in the chosen view complete, wrapping or
stacking it at narrow widths. Long status identifiers and copyable attention
commands remain intact in the output stream and may wrap visually in the terminal.
`--details` controls diagnostic depth. `--plain` removes report colors and
decoration; global `--no-color` keeps formatted layout and appropriate symbols.
Redirected reports are automatically plain and complete even with forced color.
Unsupported terminals use a plain fallback.

### Plans, submission outcomes, and inspection

`gwflow run --dry-run` and `gwflow run` show the same intended
**Task / Next action / Why** plan. Actions are Run, Continue, Retry, Finish,
Repair, Reuse, Wait, Defer, or Blocked. A Task's current state is separate from
its Next action: a failed completion job can need Finish, and a failed consumer
can need Defer until upstream results recover.

Detailed run reports include the full reason, changed inputs and commands,
attempt UUIDs, work/results/staging paths, expected producer attempts, active
consumers, public job names, available backend IDs, log paths, and planned
submissions. Previous retained-result removal is disclosed, including Repair or
transfer recovery that replaces results. Dry-run uses conditional wording and
allocates no fresh attempt UUID. A blocker in the Execution scope prevents all
new submissions for that invocation. Blocked status succeeds as inspection;
blocked run and dry-run return an error.

Actual runs report only confirmed new backend acceptances, including lifecycle
jobs, for example `Submitted 9 jobs across 3 Tasks.` A Task counts when it receives
at least one new job; that does not mean it is fully submitted or finished.
Runs with no new submissions say so. Dry-run and blocked plans never claim
submission.

Interrupted submission separates confirmed jobs, unknown acceptance, and planned
jobs not attempted. An exception after entering a backend can happen after
acceptance; it proves neither rejection nor execution failure. Already submitted
jobs may continue, and submission is not rolled back. `--details` includes
individual outcomes and available submission names and backend IDs. Reports go
to stdout; operational diagnostics and command errors go to stderr.

Status, dry-run, logs, cancellation, and cleanup preview wait for frontend bookkeeping transitions
and leave Task attempts, baselines, Completion records, work, and results intact.
Run, run preview, and status may acquire missing registry images for selected
and required upstream Tasks, including otherwise reusable Tasks. Cleanup neither
acquires nor deletes images. Observations are not a distributed snapshot: jobs
and files can change afterward, so run and cleanup recheck before acting.

### Task indexes and individual job logs

```sh
gwflow logs                         # all declared and recorded Tasks
gwflow logs hello                   # current submissions in its current attempt
gwflow logs hello other 'batch__*'   # grouped, deduplicated Task indexes
gwflow logs hello --history         # include earlier retries and attempts
gwflow logs hello__write --no-pager
gwflow logs hello__write --stderr --no-pager
gwflow run hello --dry-run --details # separate Next action and reason
```

Task indexes show the current recorded attempt and current submissions of its
computation, preparation, and completion jobs, with failures first. Each entry
identifies the public job and submission, its observed state, and availability
of stdout and stderr. Available streams have commands that read that specific
submission; indexes never concatenate streams. Earlier retry submissions and
attempts are excluded from the current index.

Add `--history` to include earlier submissions in the current attempt and all
recorded submissions in previous attempts. Each historical group identifies its
attempt, and each job entry is marked historical, keeping resolved failures
separate from current work. Retrying a job can preserve the Task attempt while
creating a distinct submission; the index shows both identities. Historical
streams use exact submission commands, so they remain readable after retries,
fresh attempts, and managed work cleanup. `--history` supports the same exact,
multiple, pattern, and bare all-Task selection, including removed Tasks.
An earlier attempt interrupted before job admission is listed with no recorded
submissions.

Task names and quoted patterns use the same selection language as other managed
commands: every selector must match, and overlapping matches appear once. With
no selectors, all declared and recorded Task names are selected. Factory groups
and job names do not expand into Tasks. Declared Tasks without an attempt report
"no recorded work". Removed Tasks remain selectable, marked "no longer declared";
restore their declaration before using the run preview. Logs fail explicitly if
a selected Task name is also a retained public job or historical submission name,
including names from previous attempts.

One exact public job or submission name reads an individual stream: stdout by
default, stderr with `-e`/`--stderr`, and direct output with `--no-pager`. Public
names follow the selected execution generation. Multiple selectors always select
Task indexes. `--stderr` on an index is a usage error because indexes already show
both streams. Missing streams are marked in indexes and are errors when requested
individually. Logs remain in `.gwf/logs/` after work cleanup; exact earlier submission
names continue to read their logs directly.
`--history` is only for Task indexes; combining it with an individual job or
submission request is a usage error. Reading an exact historical submission does
not require `--history`.

Log inspection neither acquires images nor invokes execution planning, and it
preserves Task records, work, and retained outputs. If scheduler observation fails
or times out, indexes still show recorded jobs and available streams, marking live
state unavailable. Missing or expired scheduler state never establishes inactivity.
Use the displayed `run TASK --dry-run --details` command for the planner's Next
action and reason; that separate preview retains its normal image-acquisition and
planning behavior. Human-readable reports are the supported interface, not a stable
machine-readable schema.

### Cancelling current Task work

```sh
gwflow cancel hello --dry-run       # preview current admitted jobs
gwflow cancel hello                 # request without an interactive prompt
gwflow cancel hello other 'batch__*'
gwflow cancel '*'                   # explicitly select all Tasks
gwflow run hello --dry-run --details # inspect ordinary recovery after inactivity
```

Cancellation requires explicit Task names or quoted patterns. Every selector
must match before any request is sent, and overlapping selections are addressed
once. Names come from current declarations and recorded Tasks. Removed Tasks
remain selectable and are marked "no longer declared"; a declared Task without
an attempt reports "no admitted work to cancel" successfully.

Requests address admitted computation, preparation, and completion jobs in each
Selected Task's current recorded attempt. They do not add upstream Tasks,
expand downstream, or inspect older attempts for cancellation eligibility.
Completed work and retained outputs are preserved. The backend controls the
disposition of dependent jobs; Slurm configurations need not automatically
cancel them. Select any additional Tasks explicitly when needed.

`--dry-run` shows the requests without sending them. Actual cancellation returns
after reporting backend request outcomes, without polling for jobs to stop.
An accepted request says the job may still be stopping; only available backend
evidence is reported as confirmed inactivity. Missing live state can still
permit a request when the recorded submission and backend job identity are
known and consistent. Conflicting or unresolved identities receive no request.

Other identifiable jobs are still addressed when one identity is unresolved or
one request fails. Each failure is reported, and the command exits nonzero even
when other requests succeed. Accepted requests are not rolled back. Request
acceptance never authorizes replacement, recovery, or cleanup that requires
inactivity. Cancellation does not delete work or create persistent pause state:
after the ordinary inactivity conditions hold, a later `gwflow run TASK` can
retry or recover eligible work without a resume operation.

## Containers

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
provided. See [the registry example](../examples/registry/workflow.py).

The frontend acquires a missing image through `apptainer pull` and publishes a
complete local SIF in `~/.cache/gwflow/images`. Set `GWFLOW_IMAGE_CACHE` to choose
another cache directory; this is the sole gwflow cache-location override.
Relative overrides resolve from the Workflow directory, and persisted image
bindings are absolute. Keep the cache outside managed work, results, and
bookkeeping storage, at the same shared path on frontend and compute nodes.
Frontend and compute-node CPU architectures must match. Workers execute the
cached SIF and never download it; there is no architecture override.

Pulls report their start and either completion or failure on stderr, including
during `gwflow status`, `gwflow run --dry-run`, and `gwflow run`. Failure messages include
the image reference and error. Reusing a cached image is silent.

The same explicit reference shares an entry across a user's workflows and
Task names. Different reference spellings select different entries, even if
they refer to the same registry image. A present usable entry needs neither
registry access nor Apptainer for planning; actual execution still requires
Apptainer. Tags are not polled or refreshed when their remote content moves.
Only a missing entry is pulled. A present unusable entry is reported without
replacement, and a failed pull leaves a retryable cache miss. A failed image
dependency within the Execution scope blocks new submissions for that invocation.
Images needed only by unrelated Tasks are not acquired and do not block a
selected run or status request.

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
matching observations permits ordinary retry or repair. Status and
dry-run describe these decisions without updating Task evidence; resolving a
missing registry image may invoke Apptainer on the frontend.

Restoration and reacquisition have the same comparison rules. Restoring an image
at its previous resolved path with matching size and modification time permits
cleaned Reuse, successful-branch retry, transfer continuation, or result repair
as appropriate. A missing registry image is acquired before those decisions,
even after internal work has been cleaned. Failure leaves the old lifecycle
evidence and retained results intact and blocks new submissions in the assessed
Execution scope.
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
[validation guide](validation/README.md) for setup and repeatable checks,
and the [registry acceptance record](validation/registry-images/acceptance.md)
for recorded frontend acquisition, warm-cache execution, and worker baseline
enforcement on both backends.
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

Changing any selected image refreshes the whole owning Task, even with command
hashes disabled. When its consumers are subsequently included in a run, the new
producer attempt invalidates their prior bindings even if retained-file metadata
is identical. A selected producer run does not add its downstream consumers.
Unrelated Tasks remain reusable. Adding or removing image
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

## Lifecycle and recovery

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
Inspect preparation logs with `gwflow logs copy__gwflow_prepare --no-pager`.

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

An ordinary `gwflow run` recovers interrupted finishing under the same Task attempt
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
rules. `status` and dry-run identify transfer recovery without
restarting copying or changing records. Unknown submissions remain protected.

Results and bookkeeping must survive work removal. An unchanged completed Task
can be reused without its work. A subsequent fresh attempt recreates an absent
work root with new recorded ownership before removing any previous results.
Interrupted recreation can resume; an unexplained replacement root is rejected.
Incomplete attempts, damaged results, and uncertain submissions fail explicitly when
the required ownership or execution evidence cannot establish a safe next action.
Use the managed `gwflow` commands; ordinary `gwf.Workflow` commands retain their
own behavior under `gwf`.

Interrupted submissions are reconciled by ordinary `gwflow run`. Each backend
admission has a unique job name and durable intent identifying its backend,
operation, and dependency generations. Acknowledged IDs, saved gwf tracking,
and matching execution evidence let submission continue without repeating
accepted work. The pinned local and Slurm backends can also recover activity
from an acknowledged ID when frontend tracking was interrupted.

A failure known to precede the backend call permits another admission. An
arbitrary backend exception, missing ID, or `UNKNOWN` scheduler state does not
prove rejection or inactivity. Unresolved submissions block run with the Task
and submission name; no uncertainty override is provided. Status and run preview
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
queued, use `gwflow cancel TASK` to request cancellation of its admitted work
and wait for confirmed inactivity before running again.

Retries require the original input baseline and workspace. Resource changes
permit continuation. Structural changes and enabled command changes require a
fresh attempt. With command tracking disabled, retry commands may change while
successful siblings retain earlier outputs; enabling tracking later requires
valid baselines for every selected execution. Interrupted output sets without
success evidence are rerun, and abandoned work remains owned for later cleanup.
Status and dry-run show planned retries without allocating executions.

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
gwflow run hello --force --dry-run
gwflow run hello --force
gwflow run --force
```

Force refreshes directly Selected Tasks only. Required upstream Tasks undergo
normal checks and may be reused. A selected producer run does not add downstream
consumers; when those consumers are later considered, a changed producer attempt
requires fresh computation even if retained-file size and mtime are identical.
Inner-target selectors, groups, and dependency bypass are unsupported.

Starting a fresh attempt removes that Task's previous results before submitting
jobs. Dry-run discloses this removal without changing files. Run
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
same-attempt repair; run preview reports when a later ordinary invocation must replan.
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

## Cleanup

Use `gwflow clean` to preview work cleanup. It lists every recorded attempt for
matched Tasks, including older and failed attempts, with UUIDs, workspace and
staging locations, eligibility, and reasons. Positional exact Task names or
quoted globs select current and recorded-only Tasks, without adding upstream
dependencies. A declared Task without attempts reports no recorded work. Every
selector must match; unmatched selectors fail the whole request before deletion.
Add `--delete` for noninteractive removal of eligible work.

```console
gwflow clean
gwflow clean A B
gwflow clean 'sample_*'
gwflow clean A B --delete
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

Durable cleanup intent precedes deletion, and another `gwflow clean --delete` resumes
an interrupted eligible cleanup. Valid results remain reusable even if deletion
was interrupted. Once work is marked for removal, it cannot supply a later repair;
missing or changed results require fresh computation. Work cleanup therefore gives
up repair sources, while permitting new consumers to use retained results without
recreating completed producer work.

To remove work that default cleanup protects, select exact recorded attempt UUIDs
with repeatable `--attempt` options. Find the UUIDs and workspace/staging paths in
`gwflow clean` preview or the current attempt in `gwflow run --dry-run --details`:

```console
gwflow clean --attempt UUID
gwflow clean --delete --attempt UUID --attempt ANOTHER_UUID
```

This deliberately permits inactive failed, superseded, or repairable work to be
removed. Each selected attempt's preview explains the loss of successful
intermediate progress, diagnostics stored inside work, and repair sources.
Failed work cannot continue after cleanup intent is recorded; its next computation
uses a fresh attempt and repeats successful intermediate steps too. Cleaning older
attempts leaves current results reusable. Cleaning repair sources can require
fresh computation if retained results are missing or damaged; cleanup itself does
not change those results.

Do not combine `--attempt` with positional Task selectors. Unknown selections
fail before deletion.
Explicit selection never overrides ownership or inactivity checks: active, queued,
or transferring work and unresolved submissions cause a nonzero refusal. Logs,
external inputs, retained results, and required bookkeeping survive. Interrupted
explicit cleanup resumes by repeating the same `--attempt` selection with
`--delete`, even if the attempt is no longer current.

## Change tracking

Structure is always tracked. Commands follow gwf's inherited `use_spec_hashes`
setting (default false); enable it explicitly in the workflow directory:

```sh
gwflow config set use_spec_hashes true
```

Enabling command hashes is recommended; the inherited default is unchanged.
File size and modification
time checks do not detect changes preserving both, and successful execution does
not certify output contents. Resources, packages, hidden parameters, and the
software environment have no independent invalidation component. Package code,
tool versions or runtime environment changes can therefore leave a Task reusable
when its declared structure, tracked commands, selected image identity, and file
metadata stay unchanged.
Parameters matter only through those tracked effects. Use `gwflow run NAME --force`
(or `gwflow run --force` for all Tasks) when such an untracked change requires new
computation.

## Storage

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
describes that release, not current compatibility. Use the
[compatibility guidance](#compatibility) for incompatible runs.

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

## Compatibility

Before v1.0, each release supports one authoring interface and one durable record
format: the ones documented by that release. Factories must use the managed API.
There is no cross-version Reuse, retry, or repair guarantee and no automatic
conversion, migration, or adoption of unsupported factories or records.
Initialized storage cannot be relocated. Ordinary authored top-level targets are
not supported in a managed Workflow. Genuinely incompatible future record changes
use the existing record kind/schema checks; the CLI redesign does not change them.

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

## Development and historical records

See the [validation guide](validation/README.md) for current installed-package,
container, and backend checks and the historical evidence index. The
[release guide](releasing.md) covers artifact validation and publication;
[earlier release notes](history.md) preserve the version-specific context.
