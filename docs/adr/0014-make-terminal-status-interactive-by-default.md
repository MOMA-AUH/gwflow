# Make terminal status interactive by default

For v0.8.0, `gwflow status` defaults to an interactive view in an interactive
terminal, provides an explicit option for a static report, and automatically
produces useful static output when redirected or piped. The maintainer agreed
this scope on 2026-10-17, intentionally expanding the presentation-only review in
[issue #213](https://github.com/MOMA-AUH/gwflow/issues/213) and the corresponding
restriction in [release specification #210](https://github.com/MOMA-AUH/gwflow/issues/210).
This revises the static status presentation in
[ADR 0007](0007-give-managed-workflows-a-dedicated-cli.md) before the public CLI
compatibility baseline begins; it does not change Task-selection semantics.

On 2026-10-18, the maintainer retargeted this design and the current release
issues from v1.0.0 to v0.8.0. The agreed behavior, implementation slices, and
validation requirements remain in scope. v0.8.0 remains a pre-1.0 release;
the future stable compatibility baseline in ADR 0010 is not established here.

The interactive view uses its full visual presentation, including colors and
useful progress bars; a separate monochrome interactive mode is not required.
Remove both global color flags, `--no-color` and `--use-color`, and stop consulting
the project's `no_color` configuration setting. This revises ADR 0007's
color-control decision. The interactive view retains full colors and bars even
when the shell environment sets `NO_COLOR`.

Honor `NO_COLOR` for static output: formatted reports retain their layout and
bars but omit colors when it is set. `--plain` and redirected reports remain
undecorated regardless of environment color preferences. For example, in a
capable interactive terminal, `NO_COLOR=1 gwflow status` opens the full visual
interactive view, whereas `NO_COLOR=1 gwflow status --static` prints a formatted
report with bars but no colors. Update generated command hints to omit the
removed flags. Redirected gwflow diagnostic logging must also be free of color
escapes, including at debug verbosity.

Retain the delivered state colors and symbols, with visible state words so that
color is supplementary. The maintainer explicitly approved retaining these
associations on 2026-10-18:

| States | Color |
| --- | --- |
| Pending | Magenta |
| Queued | Cyan |
| Preparing, running, finishing, active | Blue |
| Reusable, completed | Green |
| Repairable, deferred, unknown | Yellow |
| Blocked | Bold yellow |
| Failed | Bold red |
| Canceled | Red |

Use `gwflow status --static` to print one formatted report and exit, retaining
tables and useful progress bars in a capable terminal. Use `gwflow status --plain`
to print one undecorated static report and exit, including when invoked in an
interactive terminal. Redirecting or piping status output automatically selects
the plain static form. For example, `gwflow status > status.txt` and
`gwflow status | less` produce complete plain reports without opening the
interactive view. These mode choices do not change which Tasks are selected.

Automatic interactive entry requires both a capable terminal display and
available terminal keyboard input. If stdin comes from a file or pipe or
interactive input is otherwise unavailable, print a formatted static report
when stdout is a capable terminal. Redirected or piped stdout always receives
an undecorated static report. A terminal or output encoding unable to support
the required display also receives an undecorated static report. Do not open a
view that cannot receive navigation keys.

| Invocation context | Presentation |
| --- | --- |
| Capable terminal display and available keyboard input | Interactive status |
| `status --static`, capable terminal output | One formatted static report |
| `status --plain` | One undecorated static report |
| Output redirected or piped | One undecorated static report |
| Keyboard input unavailable, capable terminal output | One formatted static report |
| Terminal cannot support the required display | One undecorated static report |

When `--static` and `--plain` are both supplied, `--plain` wins: print one
undecorated report and exit. Selection, filters, and `--details` keep their
agreed meanings in each presentation mode. The color policy above applies to
formatted static fallbacks as well as explicitly requested static reports.

With no Task selectors or `--details`, the interactive view starts with the
grouped summary. Explicit Task selectors start at the matching Task rows;
`--details` starts at Task rows with their jobs expanded. These shortcuts preserve
the existing static view-selection rules, which also continue to apply to static
reports. Observable entry examples are:

| Invocation in an interactive terminal | Starting view |
| --- | --- |
| `gwflow status` | Grouped summary |
| `gwflow status '*'` | All Task rows |
| `gwflow status 'mapping__*'` | Matching Task rows |
| `gwflow status TASK --details` | That Task with its jobs expanded |
| `gwflow status --details` | All Task rows with jobs expanded |

The grouped summary remains accessible within the selected scope after entering
through a shortcut. The interactive view supports keyboard navigation to member
Tasks and their jobs and details. Browsing information
already collected during inspection reuses those observations without repeating
workflow inspection or scheduler queries. Group labels remain display labels,
not an additional Task selector language. Static reports must explain that
distinction and make valid Task selectors discoverable; interactive navigation
provides a direct route from a group to its members.

Static grouped overviews include a short discovery hint: "Groups summarize
Tasks; selectors match Task names", followed by a command such as
`gwflow status '*' --static` to list Task names. When an unmatched selector is a
known status-group label, explain that distinction and show a bounded set of
actual member Task names. Suggest a quoted Task-name pattern only after checking
that it selects exactly the group's members; otherwise provide exact-name
examples and the command for listing all Task names. Generated commands retain
relevant workflow and backend options.

For example, an unmatched `map_duplex_reads_fgbio` selector can show member names
`map_duplex_reads_fgbio__sample_A` and `map_duplex_reads_fgbio__sample_B`, with
`gwflow status 'map_duplex_reads_fgbio__*' --static` only if that pattern matches
exactly the intended members. A label that also names a real Task continues to
select that Task alone. This improves guidance without introducing group
selection or changing the requirement that every supplied Task selector match.

Use full-width drill-down screens with a visible breadcrumb trail: grouped
summary, member Task rows, a Task's summary and jobs, then a job's captured
details. During normal browsing, Up/Down selects a row, Enter opens it, Esc goes
back, `?` opens help, and `q` quits. Show the applicable shortcuts in the screen
footer. Returning to a previous screen restores its highlighted row and scroll
position. Entry through `--details` still starts with jobs expanded in the Task
list; it does not require drilling into every Task to see those jobs.

Complete the keyboard map with contextual shortcuts: `g` opens the grouped
summary within the original selection; `h` opens the selected Task's history;
`l` opens a stdout/stderr chooser for the selected job or historical submission.
Home and End move to the first and last rows, or the beginning and latest content
in logs. Page Up and Page Down page through rows or log content. Tab and Shift+Tab
move between controls and expandable sections; Enter opens the selected item or
activates the focused control. Show applicable actions in the footer and the
complete keyboard reference under `?`.

While entering search text, shortcut letters such as `q` and `r` enter text.
Enter applies the search; Esc cancels editing. Outside text entry, Esc closes
the current menu or help view before navigating back through screens. At the
root screen, Esc stays there. Preserve the existing meanings of `f`, `r`, `u`,
and log-following `F`; their applicable contexts are visible in help and footers.

Interactive list rows may abbreviate long names with an ellipsis. Detail views
and copyable commands retain the full names, wrapping where necessary. At narrow
widths, stack state and progress beneath the name and shorten the bar to fit,
while keeping the bar, numeric fraction, and state visible. For example, a row
can show `map_duplex_reads_...__sample_B` above `failed [######----] 3/5`, with the
complete Task name available by opening the item. This explicitly revises
ADR 0007's no-truncation rule for interactive list rows only; static and
redirected reports continue to preserve complete text.

The minimum supported interactive terminal size is 40 columns by 12 rows.
Use the normal layout at 80 by 24 and larger, and the compact stacked layout
down to the minimum, retaining state, bars, fractions, and access to complete
details. Keep essential shortcuts visible at narrow widths and expose the
complete keyboard reference with `?`.

When automatic interactive entry starts below that minimum, print a plain static
report and briefly explain the fallback on stderr. If an open TUI is resized
below the minimum, preserve the session and display "Terminal too small. Resize
to at least 40 × 12. q: quit". Enlarging the terminal restores the same view and
navigation position without another inspection. Validate 40 by 12, 80 by 24,
and larger screens, including resize transitions. Static reports retain their
existing narrow-terminal support and complete text.

For example, opening `map_duplex_reads_fgbio` in the grouped summary shows its
member Tasks under `Groups > map_duplex_reads_fgbio`. Opening a member shows its
summary and jobs, and opening a job shows the captured job details. Esc returns
through those screens with the previous focus and scroll position restored,
without inspecting the workflow or querying the scheduler again.

Use `/` for Task-name search and `f` for state filtering. Both operate on the
captured selection without new workflow inspection or scheduler queries. Name
search matches literal text in Task names without regard to case. This affects
only the view search; CLI Task selectors retain their existing matching rules.
State filtering starts with the command's `--status` choices and remains editable
in the interactive view. Search and state filters determine which groups and
Task rows appear. Hide groups with no matching Tasks, but keep each visible
group's bar and state counts based on all its Selected Tasks, before view
filtering. Show a separate matching count; opening a group shows only its
matching Tasks. Keep the original Task selectors fixed for the session;
clearing view filters restores the Tasks selected by those selectors rather
than adding unrelated Tasks.

For example, `gwflow status 'mapping__*' --status failed` initially shows failed
Tasks within the inspected `mapping__*` selection. Changing or clearing the
state filter reveals other already-inspected members of that selection. Display
the scope, active filters, and shown-versus-selected count, such as "Scope:
mapping__*; Showing 6 of 24 selected Tasks; State: failed; Name search: none".
Required workflow notices remain visible even when their Tasks are filtered
out. Preserve the existing primary-Task-state filtering semantics.

For a group with 14 reusable, 4 active, and 6 failed selected Tasks, filtering
to failed Tasks retains a 14/24 reusable bar, the 4 active and 6 failed counts,
and the explanation "6 Tasks match the filter". Opening that group shows those
six failed Tasks. Do not change the bar to 0/6: its denominator remains the
selected membership. This applies to interactive and static grouped summaries
and refines the earlier design statement that group counts follow view filters.

For v0.8.0, workflow-status refresh is manual only: after the initial inspection,
the user presses `r` to request a fresh status snapshot. Do not include timed automatic refresh or
an automatic-refresh toggle in this release. Browsing captured information does
not initiate refresh.

Keep the previous snapshot available for responsive navigation while showing
"Refreshing...". Replace the displayed snapshot together only after a successful
inspection. Preserve the current screen, filters, selected item, and scroll
position where possible. If the selected item disappears, return to the nearest
surviving parent and explain the change, except for pinned log and history views
as specified below. This is a presentation boundary, not a claim that source
observations form a distributed atomic snapshot.

On refresh failure, retain the previous snapshot and its timestamp, and show a
visible error. For example, "Refresh failed: scheduler unavailable" appears
alongside "Showing previous snapshot - Last inspection finished 3 minutes ago".
A failed refresh does not reset the displayed snapshot's age. Allow only one
refresh at a time; repeated `r` presses during acquisition do not queue further
inspections.

Open a loading screen before the initial inspection finishes. Show elapsed time
and meaningful activity when available, such as loading the workflow, inspecting
Tasks, waiting for frontend submission bookkeeping, or acquiring a missing image.
Loading remains responsive to `q`, which stops the inspection and exits. After
an initial inspection failure, remain on an error screen with a concrete error,
"No snapshot available", `r` to retry, and `q` to quit. For example, a syntax
error in `workflow.py` shows its source location and message; the user can fix
the file and retry without restarting status. After any successful inspection,
subsequent refresh failures retain that snapshot as described above.

Quitting the TUI with `q` returns the result of the most recent completed
inspection: zero after a successful inspection, one after a failed inspection.
A snapshot containing failed or blocked Tasks is still a successful inspection.
A failed refresh followed by quitting returns one even when an older snapshot
remains visible; a later successful refresh restores the successful exit result.
Quitting before any completed inspection returns one. If a refresh is in flight,
stop it and use the result of the previous completed inspection. An unexpected
interface failure returns one; Ctrl+C retains the existing interrupted-command
behavior. These rules define the new interactive session's exit behavior;
ordinary static commands retain their existing exit-status semantics.

On exit, restore the terminal, stop the inspection process and its owned
subprocesses, and release inspection resources and locks. This includes stopping
an image acquisition owned by the inspection, while leaving submitted workflow
jobs running. Preserve the final failure explanation on stderr when exiting
after an error. Release the frontend lock after each inspection; keeping the
TUI open for browsing must not prevent ordinary commands in another terminal.

Distinguish a failed inspection from a successfully captured blocked Task. When
ordinary status successfully reports a recognized per-Task blocker, the TUI
installs that blocked snapshot, including unavailable progress where appropriate.
Only failures of inspection itself retain the previous snapshot or leave the
initial error screen without a snapshot.

Each manual refresh reloads the workflow and its imported Task definitions as a
fresh `gwflow status` invocation would, using the same workflow location,
backend, and original Task selectors. Reapply those selectors to the new
declarations, while retaining the current view and filters where possible. An
edited computation command is reassessed, and a newly declared Task matching an
original selector enters the refreshed selection.

Removing an explicitly selected Task makes that selector unmatched; fail that
refresh visibly and retain the previous snapshot rather than silently weakening
the selection. A workflow loading or syntax error likewise leaves the previous
snapshot available with an error. The rule requiring every supplied Task
selector to match remains in force. Navigation fallback after a successful
refresh applies to items absent from the resulting view, not to invalid
selections that would fail an ordinary invocation.

A Task detail screen leads with its state, Jobs completed fraction, the
snapshot's Next action and reason, and its job list. Keep the full attempt
identifier, storage paths, and detailed planning evidence in an expandable
"Attempt, paths, and diagnostics" section. Opening a job exposes its full public
name, submission identifier, backend job ID, and log paths where those facts are
available. Expanding or opening these captured details requires no additional
workflow inspection or scheduler queries; displaying a log path does not imply
that its contents or availability have been read.

For example, `mapping__B` can show state Failed, Jobs completed 3/5, Next action
Retry, and the reason "The index job failed." Its job list shows preparation,
align, and sort completed, index failed, and completion pending. The identifiers
and paths section starts collapsed. Blockers, uncertain activity, and required
recovery guidance remain visible in the main Task view rather than being hidden
in that section. The displayed Next action describes the captured snapshot and
does not authorize execution.

The v0.8.0 TUI is for inspection only. Provide copyable ordinary-command hints in
Task details, including a run preview, ordinary run, and Cancellation, rather
than controls that execute these operations within the TUI. For example, Task
`mapping__B` exposes `gwflow run mapping__B --dry-run`,
`gwflow run mapping__B`, and `gwflow cancel mapping__B`. Users can leave status
open while issuing commands in another terminal and press `r` to inspect the
resulting state. This keeps the first interactive release focused on status,
diagnostics, logs, and history. The ordinary commands retain their current-state
validation and execution safeguards.

Use Textual for the interactive interface and retain Rich for static reports.
Textual's [screen navigation](https://textual.textualize.io/guide/screens/),
keyboard controls, tables, scrolling, and
[interaction-testing tools](https://textual.textualize.io/guide/testing/) fit the
agreed interface. Run each workflow inspection in a separate, stoppable process
so workflow imports and blocking reads cannot freeze the interface. Do not rely
on cancellation of a Textual thread worker to stop blocked inspection; its
[worker documentation](https://textual.textualize.io/guide/workers/#thread-workers)
distinguishes cancellation from terminating thread work. The log viewer needs a
bounded file reader as well as a display widget.

Validate installation through both the Python package and Conda artifact under
the supported Python 3.12 and Rich dependency range. Verify keyboard operation
through the installed command in a real terminal, alongside appropriate Textual
interaction tests. The library choice alone does not establish large-workflow
responsiveness, bounded log memory use, or successful interruption of inspection.

Use 100 Tasks and 1,000 jobs as the v0.8.0 interactive scale-test workload, as chosen
by the maintainer on 2026-10-18 to reflect expected workflows. The larger
10,000-Task/100,000-job stress-test proposal was not adopted. Use captured status
data for automated navigation, filtering, layout, and responsiveness checks at
the agreed scale. Exercise refresh, state interpretation, scheduler failures,
and absence of repeated queries through both the local backend and the real
Slurm adapter with controlled replacement Slurm commands. Extend the existing
simulator for the required TUI cases; its coverage is not evidence of production
Slurm behavior.

Run functional, interaction, resource-bound, and backend integration checks in
CI for the installed Python package and Conda artifact. Run reproducible automated
performance measurements on a documented machine and record the results
separately, without making ordinary CI depend on tight wall-clock thresholds.
Implementation work includes executing these checks and recording their results;
the maintainer is not required to collect benchmark outcomes manually.

CI runners must have no access to the production cluster. The maintainer excludes
a cluster-connected runner because this is a public repository. CI uses local
workers and simulated Slurm commands only; do not propose cluster access as a
way to satisfy these acceptance criteria. Any representative live-cluster
validation remains separate follow-up outside CI, consistent with the existing
release plan, and is not a v0.8.0 release gate. Keep automated candidate evidence
distinct from historical real-Slurm validation.

Provide in-app log and history views, loading their additional data only when
requested. Task and job details use the existing status snapshot. Opening stdout
or stderr reads the file for that specific submission. Opening Task history
reads saved records of earlier submissions and attempts, clearly distinguishing
earlier retries within the current attempt from previous attempts. Historical
views show recorded evidence without querying the scheduler merely to browse
history.

Give log and history reads their own timestamps; opening them does not refresh
the status snapshot or reset its age. Show missing files and read failures in the
relevant view. This keeps initial status acquisition focused on current
observations while providing an in-app troubleshooting path.

Open logs at their latest 200 lines, clearly indicating that only a portion is
displayed. Page Up and Page Down navigate older and newer content, reading
bounded portions and keeping memory use bounded. Provide direct jumps to the
first and last lines as well. Keep the viewer tied to the chosen submission even
if a later retry creates another submission.

Use `u` to reload the current log while preserving the reading position where
possible; when already at the end, show the latest output. In a history view,
`u` reloads the saved history records. `r` remains the separate workflow-status
refresh.

An open log or Task history view stays in place when a successful status refresh
changes its current attempt or submission or removes its Task from the refreshed
selection. Preserve the selected log, reading position, and following mode.
When a newer submission becomes current, explain that change while continuing
to identify the original submission being viewed. History keeps its independent
read timestamp, and `u` reloads its saved records.

If the Task is absent from the successfully refreshed selection, show "Task no
longer in the refreshed selection" and retain the open log or history view until
the user leaves it. Esc then returns to the nearest surviving parent in the new
status view. This is an explicit exception to navigation fallback for disappearing
items. Missing or unreadable logs retain the failure behavior specified below.
An unmatched original exact Task selector still fails the refresh and retains
the previous snapshot; it is not a successful removal from the selection.

Use Home to jump to the first log line and End to jump to the latest lines.
Provide opt-in live following, initially off, toggled with `F`. While following,
check the chosen log for appended output roughly once per second and keep the
view at the end. Scrolling upward or pressing Home pauses following; `F` resumes
it. End jumps to the latest output without enabling following. Leaving the log
view stops following. Reads remain tied to that submission and bounded in memory.
Following a log does not inspect the workflow or query the scheduler. Make this
distinction visible, for example "Live log: following; Status snapshot: 2 minutes
old". Workflow-status refresh remains manual only.

Live log following recovers automatically from changes to the chosen submission's
log path. Appended output keeps the view at the end. On detected truncation or
replacement, reload the latest bounded portion, show a clear notice, and continue
following. For example, "Log replaced — reloaded latest content" appears alongside
the exact submission and the independently aged status snapshot.

If the log is missing or unreadable, retain the currently displayed portion,
mark it as previously read, show the read failure, and retry while following is
enabled. Resume when the file becomes readable, with a visible recovery notice.
These reads stay tied to the same selected submission's log path; a retry creating
another submission does not switch the viewer to its log. Leaving the view stops
following and retries.

When following is off, detecting truncation or replacement preserves the
displayed portion and offers `u` to reload instead of unexpectedly moving someone
reading older output. Keep file changes and gaps visible rather than silently
joining old and replacement content. All reads and retained display content
remain bounded.

The TUI log viewer wraps text to the available width. Read exceptionally large
lines in bounded portions, with visible continuation markers and paging through
the remaining content. The latest 200 lines are the initial display target,
subject to a bounded read size: if the size limit is reached first, label the
smaller portion explicitly, for example "Showing latest portion — size limit
reached", with shortcuts for earlier content, the beginning, and the latest
content. A line-count limit alone is not an adequate memory bound.

Preserve ordinary log text colors and emphasis. Cursor movement and
screen-clearing sequences must not rearrange the viewer. Show visible replacement
characters for undecodable bytes, with a short notice, while keeping readable
content available. These rules affect TUI display; saved log files retain their
original contents. Display limits must not silently discard access to the rest
of a long line or require reading an entire large file into memory. Choose and
validate the exact byte limits during implementation.

Restore useful progress bars. A group-summary bar measures reusable Tasks over
Selected Tasks in that group, with its numeric fraction and other-state counts
visible. For example, a group with 14 reusable Tasks, 4 active Tasks, and 6 failed
Tasks displays a 14/24 bar and fraction alongside the active and failed counts.
The bar does not measure elapsed time or completed jobs.

Task rows and Task detail screens retain a visible progress bar for every Task
state, including blocked Tasks and Tasks whose completed-job count cannot be
established. Do not omit the bar merely because the count is unknown. Task
progress retains its glossary meaning: completed computation and lifecycle jobs
after excluding steps known to require repetition. Blocked is a Task state, not
a progress value: a blocked Task can have a known zero, partial, or full count,
or an unknown count.

When a Task's completed-job count cannot be established, use a stationary hatched
bar alongside `?/N` and a concrete reason rather than only the word "unknown".
For example, `[//////////] ?/5` can accompany "Image unavailable; progress not
assessed". The bar remains visible and distinct from a known zero such as
`[          ] 0/5`; it does not animate as if work or inspection were continuing.
An unresolved submission is another possible reason for an unavailable count.
These representations preserve uncertainty rather than inventing a completion
fraction.

For v0.8.0, counted bars have three meanings only: reusable over selected Tasks in
a group, completed over total jobs in a Task, and confirmed backend acceptances
over planned submissions during run. Individual jobs display their observed
state, such as queued, running, or completed; current observations do not provide
a within-job completion fraction.

Inspection, frontend-lock waits, scheduler waits, and image acquisition use an
activity indicator with the current phase and elapsed time when available. For
example, "Inspecting workflow… 8s", "Waiting for scheduler… 12s", or "Acquiring
image <image reference>… 32s" identifies the ongoing operation. Formatted
terminal commands show activity while waiting. Plain and redirected output use
concise phase messages on stderr without animation or repeated elapsed-time
updates. Do not present estimated completion percentages or ETAs. Task bars
remain present for every Task state as specified above; activity indicators do
not change the meaning of a captured Task's progress.

Across commands, make outcomes easy to find and reduce long identifiers and
repeated routine prose without hiding differing outcomes or cases requiring
attention. Cancellation requested and inactivity confirmed remain distinct as
required by [ADR 0009](0009-cancel-admitted-work-without-pausing-tasks.md).

Cancellation reports summarize identical routine outcomes beneath the Task name.
For example, canceling a Task whose four jobs are confirmed completed reports:

```text
Task B
  No admitted work to cancel.
  All 4 jobs confirmed inactive (completed).
```

Show differing outcomes, failed requests, and unresolved jobs individually. A
mixed result can report:

```text
Task B
  Cancellation incomplete: 1 request sent, 1 failed.
  2 jobs confirmed inactive (completed).

  align   Cancellation requested; may still be stopping.
  sort    Cancellation request failed: backend rejected request.
```

Add `cancel --details` for full per-job outcomes and available attempt,
submission, and backend identifiers. Routine identifiers move to that detailed
report; identifiers needed to investigate failures or uncertain activity remain
in the default report. Evidence of recorded success alone must say "Recorded
successful work; live state unavailable", not claim confirmed inactivity. This
changes reporting depth, not cancellation scope, safeguards, or exit behavior.
Do not advise repeating a mutating cancellation merely to obtain more detailed
output from the previous invocation.

Run reports follow the sequence plan, submission progress, and final outcome.
Keep the existing Task / Next action / Why information, with blockers and
retained-result removal prominent. A dry-run is explicitly labeled as a preview
that will not submit jobs and states the planned submission count, for example:

```text
Run preview — no jobs will be submitted

Task       Next action   Why
mapping_A  Reuse         Results reusable
mapping_B  Retry         The index job failed
mapping_C  Run           No previous attempt

Would submit 9 jobs across 2 Tasks.
```

Label an actual run's plan "Execution plan". During submission in a formatted
terminal, show a bar such as `Submitting jobs [######------------] 3/9 confirmed`.
Its numerator is confirmed new backend acceptances and its denominator is planned
submissions, including lifecycle jobs. It does not measure completed computation.
Unconfirmed acceptance does not advance the confirmed count. Plain and redirected
output preserve the plan and final outcome without an animated progress bar.

After successful submission, lead with the outcome, for example "Submitted 9 jobs
across 2 Tasks." A counted Task has received at least one new job; this does not
claim that all its jobs were submitted or completed. A run requiring no new
submissions reports "No new jobs were submitted." Interrupted submission keeps
partial outcomes prominent:

```text
Submission interrupted
  3 jobs confirmed submitted
  1 submission acceptance unconfirmed
  5 planned jobs not attempted

Already submitted jobs may continue; submission was not rolled back.
```

Identify affected jobs and available diagnostic identifiers directly beneath
failures or unconfirmed acceptances. Routine per-job receipts remain available
with the existing `run --details`. Dry-run and blocked plans never claim actual
submission; the reporting changes preserve existing execution safeguards.

Standalone Task log indexes use compact current-job rows by default, showing
job name, observed state, and availability of both stdout and stderr, with
failures first. Missing streams and unavailable live state remain visible.
For example:

```text
Task mapping_B — current attempt

Job       State       stdout      stderr
index     failed      available   available
align     completed   available   missing
prepare   completed   available   available

Read a job's logs:
  gwflow logs mapping_B__index           # stdout
  gwflow logs mapping_B__index --stderr  # stderr

Exact submissions, paths, and commands:
  gwflow logs mapping_B --details
```

Add `logs --details` for full attempt, submission, and backend identifiers,
paths, and commands pinned to exact submissions. Explain that a public job name
reads its current submission, whereas an exact submission command continues to
identify the same log after a retry. This changes the default index, which
currently prints long submission identifiers and exact commands for each
available stream. Keep explicit stdout and stderr examples in discovery guidance;
stdout remains the default stream.

`logs --history` retains explicit historical identities and pinned commands,
distinguishing earlier retries within the current attempt from previous
attempts. Individual log requests continue to display the requested stream
without report headings. Preserve existing log selection semantics, missing-log
errors, and the distinction between recorded success and confirmed inactivity.

`logs --details` applies only to Task indexes. It is valid together with
`--history` and then expands both current and historical indexes. Supplying it
with one exact public job or submission stream request is a usage error, with
guidance to use the owning Task's detailed index or read the stream without
`--details`. For example, `gwflow logs B__index --details` points to
`gwflow logs B --details` for identifiers and paths and `gwflow logs B__index`
for stdout. Preserve context options in generated commands. This follows the
existing index-only `--history` rule and leaves individual log output free of
report headings.

Cleanup reports group attempts under each Task, leading with eligibility or
outcome and its reason. Keep complete attempt UUIDs, workspace paths, staging
group paths, and observed owned staging paths in the default report. These
values support deletion review and explicit `--attempt` selection and remain
visible without a separate details option, consistent with ADR 0007. For example,
using placeholders for complete identifiers and paths:

```text
Cleanup preview — 1 eligible, 1 kept
No files removed. Use --delete to remove eligible work.

Task B
  Eligible — completed work is valid; jobs inactive
    Attempt: <full UUID>
    Workspace: <full path>
    Staging: <full path>

  Kept — older attempt; ordinary cleanup preserves it
    Attempt: <full UUID>
    Workspace: <full path>
```

Explicit-attempt cleanup keeps its consequences prominent: it can discard
successful intermediate work, diagnostics inside the workspace, and repair
sources; incomplete computation will require a fresh attempt. Retained results
and logs remain preserved. Reduce repeated prose through shared notices while
retaining each attempt's particular eligibility or refusal reason.

After `clean --delete`, lead with actual outcomes and distinguish work removed
during this invocation, already removed work, kept work, and blocked or failed
cleanup. If deletion stops after removing some work, report partial cleanup and
identify what was removed and what remains. A refusal before any deletion must
not imply partial removal. Selection, ownership and inactivity checks, and
revalidation at deletion remain unchanged; a previous preview does not authorize
later deletion.

Recovery reports lead with the outcome and keep the complete current-attempt
identifier and the exact submissions resolved in the default report. This
preserves [ADR 0013](0013-provide-explicit-recovery-for-unresolved-submissions.md),
including the interface accepting one exact Task with no additional recovery
flags or interactive confirmation. For example, with placeholders standing for
complete identifiers:

```text
Task mapping_B
  Resolved 2 submissions.
  Whole-Task inactivity asserted by the operator.

  Attempt: <full attempt UUID>
  Resolved submissions:
    <full submission name>
    <full submission name>

  No jobs submitted or canceled.
  Recovery does not establish successful computation.

Next: inspect the execution plan
  gwflow run mapping_B --dry-run
```

For a removed Task, make the next-step hint applicable by telling the user to
restore its declaration before suggesting preview/run. This corrects reporting
without adding a recovery capability.

If there is nothing to resolve, report "No unresolved submissions." Keep
scheduler-observation failures visible. Never describe an operator assertion as
independently confirmed inactivity or successful computation.

When status or run encounters unresolved submissions, list the affected Tasks
and complete submission names, followed by shared recovery guidance. Explain
that the operator must investigate the scheduler and job logs and establish that
every job in the Task is inactive, including preparation, computation, and
completion jobs. A cancellation request alone does not establish inactivity.
Only then present `gwflow recover TASK` as the command to record that assertion.
This responsibility remains visible in ordinary blocked-work guidance. Recovery
retains its existing current-attempt scope, refusal on observed queued or running
jobs, and preservation of valid partial work and submission evidence.

Expected errors use a concise explanation with the relevant Task, file and line,
resource, or backend message when available, and a concrete next step when one is
known. This includes missing files, invalid selectors, workflow syntax errors,
and recognized backend failures. Syntax diagnostics identify the relevant source
location and message, for example:

```text
Error: could not load the workflow
  workflow.py:42
  Expected ':' after the if statement.
```

Unexpected failures lead with a short summary and retain their complete
exception traceback from the same invocation. For example:

```text
Error: status inspection failed unexpectedly
  TypeError: <exception message>

Traceback:
  <complete diagnostic traceback>
```

In the TUI, keep the error summary visible and expose the traceback through
expandable error details. Obtaining diagnostic evidence must not require
repeating a mutating run, cancellation, cleanup, or recovery command. Keep
partial submission, cancellation, and cleanup outcomes visible alongside errors.

Preserve existing exit-status semantics and output channels: successful
inspection returns zero even if it finds failed or blocked Tasks; an operational
failure such as a blocked run returns one; incorrect CLI usage returns two.
Reports and partial-action receipts go to stdout, while command errors and
diagnostic logging go to stderr. For example,
`gwflow run B --dry-run > preview.txt 2> diagnostics.txt` separates the execution
preview from errors and logging.

`--details` controls workflow-report depth, including job rows, full identifiers,
paths, and planning evidence. The global `--verbose` option continues to control
logging, with `--verbose debug` providing internal diagnostic messages. Neither
option is required to obtain an unexpected-error traceback. The options can be
combined, with global options before the command, for example
`gwflow --verbose debug run B --dry-run --details`.

Retain config/workers command interfaces, workflow/backend discovery, persisted
storage, and ordinary lifecycle/selection safeguards apart from the shared
presentation changes explicitly specified here. Standalone raw stdout/stderr
log behavior remains unchanged. The maintainer explicitly approved these
unchanged areas along with the implementation breakdown on 2026-10-18.

The maintainer approved ten implementation slices and their validation criteria
on 2026-10-18, including publication and updates to #213 and
[release preparation #217](https://github.com/MOMA-AUH/gwflow/issues/217).
All ten slices are required release blockers. This records the completed design
review, not delivered features or completed release validation. The subsequent
retargeting to v0.8.0 also updates parent #210. No parent issue is closed by
the design review or release retargeting.

The approved slices were published with native prerequisite links and as
sub-issues of #213. Each also directly blocks #217:

| Implementation ticket | Blocked by |
| --- | --- |
| [#226 — Static status and Task discovery](https://github.com/MOMA-AUH/gwflow/issues/226) | None |
| [#227 — Command colors, activity, and diagnostics](https://github.com/MOMA-AUH/gwflow/issues/227) | None |
| [#228 — Interactive status browser](https://github.com/MOMA-AUH/gwflow/issues/228) | #226, #227 |
| [#229 — Refresh and filters](https://github.com/MOMA-AUH/gwflow/issues/229) | #228 |
| [#230 — Standalone log indexes](https://github.com/MOMA-AUH/gwflow/issues/230) | None |
| [#231 — In-app logs and history](https://github.com/MOMA-AUH/gwflow/issues/231) | #229 |
| [#232 — Live log following](https://github.com/MOMA-AUH/gwflow/issues/232) | #231 |
| [#233 — Run reports](https://github.com/MOMA-AUH/gwflow/issues/233) | #227 |
| [#234 — Cancellation reports](https://github.com/MOMA-AUH/gwflow/issues/234) | #227 |
| [#235 — Cleanup and recovery reports](https://github.com/MOMA-AUH/gwflow/issues/235) | #227 |
