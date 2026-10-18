# Operating and upgrading managed workflows

Use this guide for routine changes, cleanup, unresolved submissions, and version
upgrades. Commands below use `hello` from the [quickstart](../README.md); replace
it with the affected Task's exact name. Run commands in the workflow directory
with its configured environment. The [reference](reference.md) covers authoring,
command options, storage requirements, and detailed lifecycle behavior.

## Compatibility boundary

The current release work targets **v0.8.0**, which remains under the pre-1.0
policy: there is no general cross-version compatibility guarantee. The existing
installed compatibility journeys check specific baseline/candidate pairs.

The future stable compatibility baseline starts with **v1.0.0**. Across forward
upgrades within 1.x, the public authoring API and CLI remain supported. An unchanged
workflow with valid records and retained results can reuse completed Tasks,
including after internal work cleanup, without upgrade-induced recomputation or
manual record conversion. With all affected jobs inactive before upgrading,
valid partial attempts also preserve successful computation, retry, Repair, and
interrupted result installation. An upgrade alone does not require a fresh
attempt. See [ADR 0010](adr/0010-preserve-public-interfaces-and-completed-work-through-1-x.md).

This contract does not promise compatibility with every pre-1.0 record format,
downgrades, live upgrades, mixed-version frontends and workers, storage
relocation, or adoption of unowned data. The current
[compatibility evidence](validation/compatibility/README.md) is a pre-1.0
bootstrap of the validation procedure; it does not establish a released v1.0.0
artifact or prove compatibility with a future 1.x release.

The supported validation profile is Linux and Bash, Python 3.12, gwf 2.1.1,
local workers or Slurm, and deployment-provided Apptainer 1.5.4 for containers.
Run a workflow's frontend commands on one physical frontend. Jobs and the
frontend must see the same configured storage and image paths; storage must
provide coherent record visibility and atomic directory rename on the relevant
filesystem. Broader environments are unverified. See
[storage requirements](reference.md#storage) and the
[validation guide](validation/README.md) for deployment details and evidence.

## Command changes and untracked changes

Managed workflows always track computation commands. Changing command text
requires a fresh whole-Task attempt, subject to activity and ownership checks,
even when a harmless edit would produce identical results. No opt-in is needed.
An explicit `use_spec_hashes=false` configuration is rejected; remove it in the
workflow directory:

```sh
gwflow config unset use_spec_hashes
```

Package, tool, parameter, or environment changes that leave tracked declarations,
commands, image dependencies, and file observations unchanged do not invalidate
Reuse automatically. Identify the Tasks whose computation must change, then
preview and request force explicitly:

```sh
gwflow run hello --force --dry-run
gwflow run hello --force
```

Force applies to the directly Selected Tasks. Their upstream dependencies undergo
normal validity checks; downstream consumers are not selected automatically.
`gwflow run --force` selects every declared Task. Force still respects unresolved
submissions, activity, ownership, and consumer protections.

The preview names the full Execution scope and planned job/receiving-Task counts.
An actual run shows its plan, confirmed backend acceptances, and final outcome.
The terminal submission bar includes preparation and completion jobs; it does
not measure completed computation. If submission is interrupted, accepted jobs
may continue. Use the failed or uncertain submission identities in that receipt
to investigate; `run --details` requests all receipts for an invocation. Do not
repeat a mutating run merely to obtain earlier diagnostic output.

A fresh attempt removes the selected Task's previous retained results before
replacement jobs are submitted. A later failure does not restore those results.
When downstream consumers are considered by a later run, the changed producer
attempt requires them to start fresh even if the new files have identical
metadata. Let the producer's work settle, then preview and run the consumers or
the whole workflow. See the [force rules](reference.md#lifecycle-and-recovery),
[mandatory-tracking decision](adr/0011-track-command-changes-by-default.md), and
[untracked-change decision](adr/0012-use-explicit-force-for-untracked-changes.md).

## Clean completed work

After the Task is reusable, inspect eligible disposable work before deleting it:

```sh
gwflow status hello
gwflow clean hello
gwflow clean hello --delete
gwflow run hello --dry-run
```

The preview summarizes eligibility and says no files were removed. Under each
Task, review the outcome/reason, full attempt UUID, workspace, staging group, and
owned staging paths, including kept older work. Use `--plain` for an undecorated
report. Deletion distinguishes newly removed work from already removed or kept
work; interrupted deletion retains known removals and unconfirmed paths. Repeat
the same deletion command only after investigating the error.

Cleanup preserves retained results, logs, and the bookkeeping needed for Reuse.
An unchanged Task can remain reusable and provide inputs to a newly added
consumer after cleanup. Cleanup gives up intermediate files that could otherwise
Repair missing or damaged retained results; without valid repair sources,
invalid results require fresh computation. Keep results and `.gwf/` bookkeeping
together. See [cleanup](reference.md#cleanup).

## Resolve an unresolved submission

In terminal status, select a Task and press `h` for its recorded history. Select
a job or historical submission and press `l` for stdout/stderr. These reads do
not query the scheduler or refresh workflow status. Use `u` to reload the open
evidence and `r` to inspect the workflow again. The log remains tied to its exact
submission after a retry; a notice identifies changes to current work. Missing
logs and read failures stay visible, with previously read text preserved. See
the [browser reference](reference.md#status-views-and-progress) for paging and
read limits.

Use uppercase `F` to follow that submission's log. Following starts off, checks
about once per second, and recovers visibly from replacement, truncation, and
temporary read failures. Scrolling upward or Home pauses it; `F` resumes, while
End only jumps to latest. Leaving the log stops following and retries. Workflow
status still changes only when you press `r`.

An interrupted admission, missing acknowledgement, or unavailable scheduler
history can leave work blocked. Start with the normal diagnostics:

```sh
gwflow status hello --details
gwflow logs hello --details
gwflow logs hello --history --details
gwflow run hello --dry-run --details
```

Use the reported submission names, backend IDs, logs, and scheduler or worker
records to investigate **all computation and lifecycle jobs in the Task**,
including queued preparation and completion jobs and earlier retries in its
current attempt. A failed Task can still have a running sibling. An `UNKNOWN`
state or missing scheduler entry alone does not establish inactivity. If a job
can still run, finish it or request cancellation and independently confirm that
it has stopped. When sufficient backend or execution evidence becomes available,
inspect the preview again; ordinary run can reconcile those submissions.

Only after independently establishing that the whole Task is inactive, invoke:

```sh
gwflow recover hello
gwflow run hello --dry-run
```

Inspect the preview, then use `gwflow run hello` to continue eligible work.

`recover` takes one exact Task name, no extra arguments or recovery flags, and
asks no confirmation. **Invoking it asserts whole-Task inactivity**, including
when machine-verifiable evidence is unavailable. It refuses any observed queued
or running computation or lifecycle job in that Task, even an unrelated sibling.
It does not cancel jobs, submit jobs, or establish successful execution.

Recovery preserves original submission evidence, durably records the operator
intervention, and leads with the resolved count and your inactivity assertion,
then the full current-attempt UUID and exact submission receipts. Nothing
unresolved is an explicit no-op. If recovery is interrupted, confirmed receipts
remain visible and incomplete writes are marked unconfirmed. Investigate before
repeating recovery. For a Task removed from the workflow, restore its declaration
before preview/run. Recovery addresses all unresolved
submissions in the current recorded attempt, including earlier retries, while
leaving historical attempts unchanged. A resolution applies only to those
submissions; it cannot authorize later submissions or suppress newly observed
activity. The subsequent ordinary preview and run retain verified successful
work and decide what to retry, Repair, or finish. Input, command, ownership, and
consumer checks still apply; unchecked files do not become Completion evidence.
See [recovery details](reference.md#lifecycle-and-recovery) and
[ADR 0013](adr/0013-provide-explicit-recovery-for-unresolved-submissions.md).

## Upgrade with all jobs inactive

1. Coordinate a stop to new submissions for every workflow using the environment
   being changed. In the old environment, record `gwflow --version` and inspect
   `gwflow status '*' --details` and `gwflow logs '*' --history --details`. Status covers
   declared Tasks; logs and cancellation can also address recorded Tasks removed
   from the declarations. Check those Tasks' admitted jobs too.
2. Finish all affected work, or preview `gwflow cancel '*' --dry-run` and request
   `gwflow cancel '*' --details` to retain every per-job receipt from that
   invocation. Default reports summarize routine outcomes and keep failure or
   uncertainty identities visible. Confirm through scheduler or worker observations that
   **every affected queued or running computation and lifecycle job is inactive**.
   Cancellation requests alone are insufficient. Resolve uncertainty through
   investigation; do not infer inactivity from a failed Task, a missing ID, or a
   successful cancellation request.
3. Stop local worker processes after their jobs are inactive. Keep the workflow,
   bookkeeping, work, retained results, staging, and images at their established
   storage locations. Do not delete records, move storage, or run old and new
   frontends against it concurrently.
4. Install the chosen supported release in the environment used by the frontend
   and all affected jobs. Use the same gwflow version for frontend and workers.
   Review that release's compatibility and deployment notes; preserve the
   supported Python, gwf, and container-runtime constraints. Remove any explicit
   `use_spec_hashes=false` setting with `gwflow config unset use_spec_hashes`.
5. Verify `gwflow --version`, restart local workers if applicable, then inspect
   `gwflow status '*' --details` and `gwflow run --dry-run --details` before an
   ordinary `gwflow run`. Valid completed work should reuse; eligible partial
   work should retry or finish without repeating verified computation. Do not
   force solely because gwflow was upgraded. If unresolved submissions remain,
   follow the investigation and explicit recovery procedure above before running.

For incompatible pre-1.0 records, use the matching old release for supported
inspection or cleanup and start new runs in separate workflow and storage
locations. Deleting bookkeeping alone cannot make old results adoptable. Keep
the old environment available for those runs; do not use an unsupported mixed
environment to repair them.

## What Completion and validation establish

Completion requires successful checked computation for the accepted inputs and
installation of the complete retained-output set. File existence, timestamps,
or scheduler success alone are insufficient. The Completion record supports
later Reuse after work cleanup. These checks do not certify scientific output
correctness, and metadata observations are not content hashes or input snapshots.
The [glossary](../CONTEXT.md) and [lifecycle reference](reference.md#lifecycle-and-recovery)
define these terms consistently.

The [compatibility suite](validation/compatibility/README.md) exercises installed
versions against unchanged storage, including completed and partial work.
Current automated local/container checks and historical live-Slurm observations
are identified separately in the [validation index](validation/README.md).
Representative production-pipeline acceptance on the actual cluster remains
follow-up work; these records do not claim it has been performed for a v0.8.0
candidate. The [final CLI presentation review (#213)](https://github.com/MOMA-AUH/gwflow/issues/213)
and its resulting work remain a release milestone. See the
[release acceptance plan](releasing.md#release-acceptance); this guide does not
settle final terminal layouts or declare the release ready.
