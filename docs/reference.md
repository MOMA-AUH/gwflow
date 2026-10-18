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
`cancel`, `recover`, `clean`, `config`, and `workers`.

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

`status` opens an interactive browser when stdout supports terminal decoration,
keyboard input is available, and the terminal is at least 40 columns by 12 rows.
The browser uses full color, including when `NO_COLOR` is set. `status --static`
prints one formatted report with progress bars and numeric fractions. `--plain`
prints an undecorated report and wins if both options are supplied. Redirected
or piped output is always plain. Unavailable keyboard input selects a formatted
static report; unsupported terminals and encodings select plain output. A
terminal too small for automatic browser entry gets a brief stderr explanation
and a plain static report.

| Request | View |
| --- | --- |
| `gwflow status` or a filter-only request | Grouped overview |
| `gwflow status '*'` or any explicit Task selectors | Flat selected Task rows |
| `gwflow status --details` with or without selectors | Selected Tasks expanded with their jobs |

In the browser, Enter opens the selected group, Task, or job on a full-width
screen. Breadcrumbs show the current location. Task details lead with state,
Jobs completed, Next action, and the concrete reason; expandable sections show
attempt identities, paths, and diagnostics. Job details retain the captured
submission state and identity even when that work must repeat for Task progress.
Individual jobs have states, without computation percentages. Ordinary command
hints include copy buttons, retaining full Task names and workflow/backend
options; clipboard support depends on the terminal. The browser executes no
run, cancellation, cleanup, or recovery commands.

| Key | Action |
| --- | --- |
| Up / Down | Select a row or scroll details |
| Enter | Open the selected row or activate a control |
| Esc | Return with focus and scroll position preserved; stay at the root |
| Tab / Shift+Tab | Move between controls and expandable sections |
| Home / End, Page Up / Page Down | Navigate longer lists and detail panes |
| `g` | Return to groups within the original Task selection |
| `/` | Edit a literal, case-insensitive Task-name search; Enter applies, Esc cancels |
| `f` | Edit primary Task states; Space toggles, Enter applies, Esc cancels, `c` clears choices |
| `r` | Inspect again using the original workflow, backend, and Task selectors |
| `h` | Read the selected Task's saved history |
| `l` | Choose stdout/stderr for the selected job or historical submission |
| `u` | Reload the current log/history without inspecting status |
| `F` | Toggle following of the selected log; initially off |
| `?` | Show the keyboard reference and captured inspection diagnostics |
| `q` | Quit status |
| Ctrl+C | Interrupt using ordinary CLI behavior |

The browser captures one inspection in a separate, stoppable process. Loading
shows the current phase and elapsed time. Workflow stdout and diagnostics are
captured for inspection from help. The frontend lock is released after capture;
navigation and filters never import the workflow again or query the scheduler.
Only `r` starts another inspection; repeated requests while busy are ignored.
There is no timed refresh. A refresh imports current definitions in a fresh
process and reapplies every original selector. Newly matching Tasks can enter;
any now-unmatched selector makes refresh fail. The old snapshot stays browsable
while inspection runs. Success replaces the observations together, preserving
the current view, filters, stable item focus, and scroll where possible. If an
item disappears, status returns to the nearest surviving parent and explains why.
Open log and history views remain pinned, with a notice when their Task leaves
the refreshed selection or current work changes. Esc then finds the nearest
surviving parent. A now-unmatched original selector still makes refresh fail.

History reads only the selected Task's saved records, distinguishing current
recorded submissions, earlier retries within the current attempt, and previous
attempts. Empty attempts remain visible, including after work cleanup. The
recorded states are evidence from disk, without new scheduler observations.
Enter or `l` on a history submission opens its stream chooser. Choosers and logs
show complete Task, attempt, public-job, and exact-submission identities; scroll
their details to read long names and paths.

Logs initially show up to the latest 200 newline-delimited lines. A 64 KiB byte
window can contain fewer lines or only part of one exceptionally long line;
the read notice identifies that limit. Page Up/Down scroll within the portion
and read the adjacent portion at its edge. Home/End jump directly to the
beginning/latest, without scanning the file for a total line count. Text wraps,
and lines longer than 2,048 characters are segmented with continuation markers.
Paging preserves UTF-8 character boundaries and keeps every portion accessible.

Each read has its own timestamp. `u` preserves the log's reading position where
possible, or loads the latest output when already at the end. It reloads saved
history without changing the status snapshot or its age. `r` leaves independently
read text, history, and their positions intact. Following starts off. A missing or
unreadable stream shows its failure and preserves any previously read portion.
While following is off, detected replacement, truncation, or changes to the previous portion preserve
the displayed text and offer `u`; paging never joins old and replacement content.
Tab focuses the read details, where arrows scroll complete notices and errors
even at 40 × 12. Read failures do not change the latest inspection's exit result.

Uppercase `F` enables live following of the selected exact-submission path,
checking about once per second and keeping the latest bounded portion at the
end. The visible following/paused state is independent of the status snapshot's
age. Up, Page Up, Home, upward mouse-wheel scrolling, and upward scrollbar
movement pause following; `F` resumes it. End jumps to latest without enabling
following. `u` while following also reads the latest portion. Leaving the log,
including opening Help, filters, or a terminal-size overlay, pauses following
and stops its pending read and retries. Returning leaves it paused.

Following reloads the latest portion after detected replacement, truncation, or
rewriting, and retains a visible change notice. If output advances beyond the
previous portion, a notice directs readers to Home/Page Up for earlier content.
Missing or unreadable files preserve the displayed text, marked as previously
read; retries continue only while following is enabled. A visible recovery notice
appears when the path becomes readable. `r` preserves following and the exact
selected submission, even when current work changes or the Task disappears.
Following and recovery never import the workflow or query the scheduler.

The reader uses at most 128 KiB + 16 bytes of log I/O per request, including
the previous-window consistency check and a few bytes of UTF-8 context. A raw
window is at most 64 KiB + 6 bytes. The decoded and rendered representations
each retain at most 196 KiB of log text, including visible replacements and
continuation markers; these bounds exclude widget/style metadata and layout
caches. Only one read per view is in flight. A separate, stoppable process owns
blocking file I/O and releases its files on completion or when the view closes.
The same read and retained-text bounds apply during following and recovery;
each successful read replaces the previous portion rather than accumulating it.
Ordinary ANSI colors and emphasis survive; cursor movement, screen clearing,
and other terminal controls are omitted or shown visibly. Undecodable UTF-8
bytes appear as `�` with a notice. Saved files and standalone raw-log commands
retain their original content.

Search and state filters narrow the captured selected Tasks; clearing both
restores the original selector scope. Search treats characters such as `*`
literally. While typing, shortcut letters enter text. State choices start from
`--status`; selecting no states shows all primary states. Scope, active filters,
and matching/selected counts remain visible. Groups retain their full selected
membership for progress and state counts, with a separate filter-match count.
For example, a 24-Task group with 14 reusable, four active, and six failed Tasks
still shows 14/24 and those counts when the failed filter exposes six matches.
Required workflow notices remain visible when their affected Tasks are hidden.

Snapshot age measures time since the last successful inspection. A failed
refresh retains those observations and their age, with a visible error; Tab
focuses the error banner and Enter opens complete error details. A later
successful refresh clears the failure. An initial operational failure shows
its concrete error and expandable complete traceback for unexpected failures;
no snapshot is shown as successful; `r` retries. Observed blocked Tasks are a valid snapshot.

Quitting stops an unfinished inspection and its owned subprocesses, releases
inspection resources, and restores the terminal. Already submitted workflow
jobs continue. Exit status follows the latest completed inspection: zero for
success, one for failure or before any completion. Quitting during refresh uses
the previous completed result. Final inspection errors remain on stderr. Unexpected UI
failures restore the terminal and report the full exception chain on stderr.
Shrinking below 40 × 12 shows a resize notice; enlarging restores the current
view without another inspection. Long list names may use ellipses; full names
remain available in details and copied commands.

Resolve Task selectors before `--status` filtering. A valid selection emptied by
the filter succeeds with an empty report (`No Tasks match the filter.` in the
grouped overview); an unmatched selector fails.
Filtering matches the primary Task state, so a blocked Task containing failed
jobs matches `blocked`, not `failed`.

The overview groups the same qualified factory implementation and effective
name prefix. A default use and an alias matching that default share a group;
other aliases remain separate. Names are not split to infer groups. A unique
prefix displays alone; shared prefixes display `prefix@module.qualified_factory`.
These are display labels, not selectors, and are determined before filtering.
Grouped reports include `gwflow status '*' --static` to list Task names.
An unmatched group label shows up to five actual member names and, only when
it selects exactly that group's members, a quoted Task-name pattern. A label
that is also an actual Task name selects that single Task.
Groups rank by their earliest member in dependency order, with declaration-order
ties; filtering an early member does not reorder surviving groups.

The header reports selected Tasks against all declared Tasks. A group's Reusable
fraction counts reusable Tasks over its selected members. Overview summaries
combine preparing, running, and finishing as `active`; queued work stays separate.
State filters hide groups without matching Tasks, but each visible group's
fraction and state counts cover its complete selected membership before filtering.
The report identifies how many Tasks match separately. Filtering the group below
to failed Tasks therefore keeps `14/24`, `4 active`, and `6 failed`, and adds
`6 Tasks match the filter`.
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
Every Task state retains a progress bar in formatted reports. Unknown completion
appears as `?/N` with a stationary hatched bar and a concrete explanation, such
as image unavailability or an unresolved submission. It is distinct from known
zero progress; plain reports retain the fraction and reason without a bar.

The primary states are pending, queued, preparing, running, finishing, reusable,
repairable, deferred, failed, canceled, and blocked. Blockage takes precedence
over failure, then cancellation, then ongoing work. Diagnostics still include
jobs running inside a failed, canceled, or blocked Task. Historical backend
failures do not override valid Completion and Reuse.

`--details` places each Task's preparation, computation, and completion jobs below
its row. Child rows show job names and states. In a static report, this does not
add attempt paths or full planner diagnostics; use
`gwflow run TASK --dry-run --details` for those. The interactive browser exposes
them in Task and job details. Both modes use captured observations; only an
explicit interactive refresh obtains newer observations.

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

Run and static status keep all text included in the chosen view complete,
wrapping or stacking it at narrow widths. Long static status identifiers and copyable attention
commands remain intact in the output stream and may wrap visually in the terminal.
`--details` controls diagnostic depth. `--plain` removes report colors and
decoration; `NO_COLOR=1` keeps formatted static layout, bars, and symbols without color.
Global `--no-color` and `--use-color` have been removed. The project `no_color`
setting is ignored, without rewriting configuration files.
Redirected reports and diagnostic logging are automatically undecorated.
Unsupported terminals use a plain fallback.

Inspection, scheduler waits, frontend-lock waits, and image acquisition show
their current phase and elapsed time on formatted terminals. Plain or redirected
commands write concise phase messages to stderr without animation or timer spam.
Cached images are reused without acquisition just to display activity.

Expected command errors include relevant source or resource information and
known next steps. Syntax errors identify the file and line. Unexpected failures
include their full exception chain and traceback from the same invocation;
neither `--details` nor `--verbose debug` is needed to obtain it. Partial-action
receipts remain on stdout and errors/logging on stderr. Inspection still succeeds
when it observes blocked or failed Tasks; operational failures exit with one and
usage errors with two. Raw individual log content is preserved.

### Plans, submission outcomes, and inspection

`gwflow run --dry-run` and `gwflow run` show the same intended
**Task / Next action / Why** plan. Actions are Run, Continue, Retry, Finish,
Repair, Reuse, Wait, Defer, or Blocked. A Task's current state is separate from
its Next action: a failed completion job can need Finish, and a failed consumer
can need Defer until upstream results recover.

The preview is labeled **Run preview — no jobs will be submitted** and gives
planned submission counts, for example `Would submit 9 jobs across 3 Tasks.`
An actual run starts with **Execution plan**. Both show the full Execution scope,
including required upstream Tasks. A blocked plan shows its blockers and omits
submission counts that could be mistaken for an executable plan.

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

During formatted terminal submission, a bar shows confirmed new backend
acceptances over planned submissions, including preparation and completion
jobs. For example, `3/9 confirmed` counts acceptances, not finished computation.
Unconfirmed acceptance never advances the bar. Plain, redirected, and unsupported
terminal output retain the plan and outcome without animation. `NO_COLOR` keeps
the bar while omitting its colors.

Interrupted submission separately lists confirmed jobs, unconfirmed acceptance, and planned
jobs not attempted. An exception after entering a backend can happen after
acceptance; it proves neither rejection nor execution failure. Already submitted
jobs may continue, and submission is not rolled back. Failed or uncertain jobs
keep available full attempt, submission, and backend identifiers in the default
receipt, including failures after confirmed acceptance. `--details` includes all
individual outcomes; routine per-job backend announcements are available through
`--verbose debug`. Reports go to stdout; operational diagnostics and command errors
go to stderr. These receipts come from the same invocation; do not repeat a run
to obtain diagnostics for an earlier submission.

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
gwflow logs hello --details         # full identities, paths, pinned commands
gwflow logs hello other 'batch__*'   # grouped, deduplicated Task indexes
gwflow logs hello --history         # include earlier retries and attempts
gwflow logs hello --history --details # expand current and historical indexes
gwflow logs hello__write --no-pager
gwflow logs hello__write --stderr --no-pager
gwflow run hello --dry-run --details # separate Next action and reason
```

Task indexes show the current recorded attempt and current submissions of its
computation, preparation, and completion jobs, with failures first. Compact rows
identify each public job, its observed state, and availability of stdout and
stderr. Guidance includes commands for both streams and a separate run preview.
Add `--details` for the full attempt and submission names, backend identity and
job ID, stream paths, and commands pinned to each available exact submission.
Missing streams remain visible, including their expected paths when a submission
is recorded. Indexes never concatenate streams. Earlier retry submissions and
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
submissions. The current rows stay compact; historical rows retain full attempt
and submission identities and pinned stream commands. Combine `--history` with
`--details` to expand both current and historical indexes with backend identifiers
and paths.

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
names follow the current submission, whereas exact submission names keep pointing
to the same log after a retry. Multiple selectors always select
Task indexes. `--stderr` on an index is a usage error because indexes already show
both streams. Missing streams are marked in indexes and are errors when requested
individually. Logs remain in `.gwf/logs/` after work cleanup; exact earlier submission
names continue to read their logs directly.
`--history` and `--details` are only for Task indexes; combining either with an
individual job or submission request is a usage error. For example,
`gwflow logs hello__write --details` points to `gwflow logs hello --details` for
identifiers and paths and `gwflow logs hello__write` for stdout. Generated commands
preserve explicit workflow and backend options. Reading an exact historical
submission does not require `--history`. Individual streams retain their normal
paging or `--no-pager` behavior without index headings.

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
gwflow cancel hello --details       # request and keep every per-job receipt
gwflow cancel hello other 'batch__*'
gwflow cancel '*'                   # explicitly select all Tasks
gwflow run hello --dry-run --details # inspect ordinary recovery after inactivity
```

Cancellation requires explicit Task names or quoted patterns. Every selector
must match before any request is sent, and overlapping selections are addressed
once. Names come from current declarations and recorded Tasks. Removed Tasks
remain selectable and are marked "no longer declared"; a declared Task without
an attempt reports "No admitted work to cancel" successfully.

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

Reports lead with each Task's outcome and counts. Identical routine outcomes
share a summary, such as "No admitted work to cancel" followed by "All 4 jobs
confirmed inactive (completed)". Mixed outcomes identify the affected public
jobs individually. Recorded success without current inactivity evidence says
"Recorded successful work; live state unavailable." It does not establish that
the job has stopped.

Add `--details` to the original cancellation invocation for every per-job outcome
and available full attempt, submission, and backend job identities. Default
reports retain these identities for failures or uncertainty. Do not repeat a
mutating cancellation to obtain details about a previous invocation. `--plain`
prints undecorated output; redirected reports are automatically plain. Receipts
go to stdout, while operational errors and diagnostic tracebacks go to stderr.
An interrupted invocation keeps accepted requests, unconfirmed request outcomes,
and the current Task's unprocessed-job count visible.

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
observed fields can remain undetected. Managed Completion still requires checked
execution and retained-set installation; these observations do not certify
scientific output correctness.
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
and submission name. Status and run preview show the same decision without
modifying managed records. Later matching execution evidence can resolve
uncertainty automatically, and expired scheduler history does not invalidate
checked Completion. Retries require inactivity for every execution being replaced.

For uncertainty that cannot be resolved automatically, investigate the scheduler,
job logs, and execution environment. Establish that **all jobs in the Task are
inactive**, including preparation, computation, completion, and queued jobs.
Requesting cancellation alone does not establish this. Then use:

```sh
gwflow recover hello
gwflow run hello --dry-run
gwflow run hello
```

`recover` accepts one exact Task name and no recovery flags or extra arguments.
There is no interactive confirmation: invoking it is the operator's assertion of
whole-Task inactivity, including when automatic observations are unavailable.
Any observed queued or running job in that Task prevents recovery, even an
unrelated successful sibling. Recorded Tasks removed from the workflow can be
recovered; restore their declarations before previewing or running them.

Recovery starts with the resolved count and the operator's whole-Task inactivity
assertion, followed by the full current-attempt UUID and every exact resolved
submission. It preserves original submission evidence and records each
intervention durably. It covers
all unresolved admissions in the current recorded attempt, including earlier
retry submissions in that attempt, and leaves historical attempts unchanged.
Repeated invocation with nothing unresolved explicitly reports a no-op. If a
write or backend close fails, the report retains confirmed resolutions and marks
any interrupted write as unconfirmed; investigate the error before repeating
recovery. Backend-observation failures remain visible. Resolution applies only
to those admissions; new submissions and later positive activity remain subject
to ordinary checks.

Status and run list every affected Task and complete unresolved submission name,
then give one shared investigation notice and context-preserving recovery commands.

Recovery submits no jobs, cancels none, and does not establish successful
execution or Completion. Ordinary preview/run determines retry, Repair, or
finishing from checked work. Successful siblings remain valid when their inputs
and commands match; unchecked outputs never become successful by assertion.
Changed commands or inputs, missing valid sources, storage ownership, and active
or uncertain consumers still govern subsequent actions. This is not a generic
repair command for corrupted or unowned state.

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
permit continuation. Structural changes and command changes require a fresh
whole-Task attempt, including successful siblings. Unchanged commands retain
eligible retry progress. Interrupted output sets without success evidence are
rerun, and abandoned work remains owned for later cleanup.
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
fresh attempt and recomputes. Changed commands also require fresh computation
instead of Repair or continuation of interrupted finishing. Consumers with an
existing input baseline defer comparison during
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

Use `gwflow clean` to preview work cleanup. The report summarizes eligibility and
states that no files were removed. It groups every recorded attempt under its
Task, including older and failed attempts, with outcome/reason first, full UUIDs,
workspace and staging-group paths, and observed owned staging paths. These details
remain complete in narrow terminals and with `--plain`; redirected reports are
automatically plain. Positional exact Task names or
quoted globs select current and recorded-only Tasks, without adding upstream
dependencies. A declared Task without attempts reports no recorded work. Every
selector must match; unmatched selectors fail the whole request before deletion.
Add `--delete` for noninteractive removal of eligible work.

```console
gwflow clean
gwflow clean A B
gwflow clean 'sample_*'
gwflow clean A --plain
gwflow clean A B --delete
```

Default cleanup removes only current completed work whose retained results still
match checked Completion and whose jobs are inactive. Failed, partial, superseded,
transferring, and repairable work is kept. Active jobs are skipped; unresolved
submissions and insufficient ownership cause requested deletion to fail. A prior
preview does not authorize a later deletion: ownership, result validity, and
activity are checked again under the same frontend guard as submission.

Deletion reports count work removed in this invocation separately from already
removed, kept, and blocked/failed attempts. An initial refusal removes nothing.
An interruption preserves known removed paths and identifies remaining or
unconfirmed work, including a directory that may be partly removed. Read the
reported error before resuming deletion.

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
removed. A shared notice explains the loss of successful
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

Managed Tasks always track structure and computation commands. No configuration
is needed. A command edit requires a fresh whole-Task attempt, including after
cleanup or when successful partial work exists. Harmless command-text edits can
also cause recomputation. Existing activity, ownership, and consumer checks still
apply.

Explicit `use_spec_hashes=false` is rejected. Remove that setting in the workflow
directory before using managed commands:

```sh
gwflow config unset use_spec_hashes
```

Explicit true remains accepted. Ordinary workflows run through `gwf` retain its
separate command-tracking behavior.

File size and modification time checks do not detect changes preserving both,
and successful execution does not certify output contents. Resources, packages, hidden parameters, and the
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

The current release work targets v0.8.0 and follows the pre-1.0 policy below.
The installed compatibility checks do not establish a stable release baseline
or a general guarantee across pre-1.0 versions.

Starting with the future v1.0.0 baseline, forward upgrades within 1.x preserve
the public authoring API and CLI, completed work after cleanup, and valid partial attempts without
upgrade-induced recomputation or manual record conversion. All affected queued
and running computation and lifecycle jobs must be confirmed inactive before
changing environments; a cancellation request alone does not establish this.
Live upgrades, mixed-version frontend/worker operation, downgrades, and storage
relocation are outside that contract. Follow the
[operating and upgrade guide](operating.md) for the procedure, supported profile,
recovery responsibilities, and current baseline-evidence limits.

Before v1.0, each release supports one authoring interface and one durable record
format: the ones documented by that release. Factories must use the managed API.
There is no cross-version Reuse, retry, or repair guarantee and no automatic
conversion, migration, or adoption of unsupported factories or records.
Initialized storage cannot be relocated. Ordinary authored top-level targets are
not supported in a managed Workflow. Genuinely incompatible future record changes
use the existing record kind/schema checks; the CLI redesign does not change them.

Finish or cancel existing jobs and confirm inactivity before switching versions.
Use the matching old release for supported inspection or cleanup of old runs. When
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
