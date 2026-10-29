# Limit initial JSON output to status

Add `--json` to `gwflow status` for scripts and monitoring.
Limit the initial JSON interface to status, leaving execution previews and
operation receipts outside its scope.
This meets the identified inspection need while keeping the new interface small.

JSON status reports contain a flat list of individual Task records, including
when no Task selectors are supplied. The report structure stays the same for
the whole workflow, name patterns, and individual Tasks. Consumers can aggregate
these records when they need group summaries.

Each Task summary includes its Task name in `name` and its currently recorded
attempt's identifier in `attempt_id`. Use the Task name accepted by CLI
selectors. `attempt_id` is a string, or `null` when no attempt exists. Retries
and Repair retain the attempt ID; starting a fresh attempt changes it. These
fields let scripts distinguish a Task from its particular attempt.

If status plans a fresh attempt while an older attempt is still recorded,
report the recorded attempt's ID. Inspection does not allocate an ID for
future work. The replacement ID appears when the new attempt is created.

Successful inspection reports contain the top-level fields `schema_version`,
`tasks`, and `notices`. Both lists are always present, even when empty. Omit
`error` from successful reports.

Error reports contain `schema_version` and an `error` object with string fields
`code` and `message`. Omit `tasks` and `notices` from error reports. These layouts
let readers distinguish saved reports from saved errors without the original
process exit code.

Allow Task selectors, `--status` filters, and `--details` with JSON because they
control report content. Reject explicit combinations of `--json` with `--plain`
or `--static` as command-line usage errors. Those options select text displays.

By default, JSON status includes Task summaries without individual job records.
With `--details`, each Task includes a `jobs` list for its computation targets
and lifecycle jobs, with their states and submission identifiers. Omit `jobs`
when `--details` is absent. This keeps routine monitoring reports compact and
follows the existing purpose of `--details`.

With `--details`, also include observed jobs removed from the current Task
definition when they remain queued, running, or have an unresolved submission
outcome. Use observations from the currently recorded Task attempt. Removing a
target declaration does not stop its submitted job, and keeping that job visible
can explain why the attempt cannot yet be replaced safely.

Each job record includes the boolean `in_current_definition`. Current
computation and lifecycle jobs use `true`; the additional removed jobs use
`false`. Exclude removed jobs from Task progress counts. Their states describe
the surviving submission without applying repetition requirements from the new
Task definition. Omit a removed job once its outcome is known to be inactive.
This addition does not enumerate a complete history of earlier attempts.

Each job record includes `name`, `submission_name`, and `backend_id`. `name` is
the Public job name, a string that stays stable across attempts and retries.
`submission_name` identifies the specific recorded submission and can select its
exact logs. `backend_id` identifies its job in Slurm or another execution backend.
Represent both identifiers as strings, or `null` when unavailable. These fields
let scripts connect a stable job to its exact logs and scheduler entry.

Retrying a job creates a new Submission name while retaining its Public job
name. Status reports recorded identifiers without allocating IDs for planned
retries. A Submission name can exist before the scheduler accepts the job;
its presence alone does not establish scheduler acceptance.

Each job record includes `state` and `observed_state`. `state` follows ordinary
status rules, including work that must be repeated. `observed_state` describes
the recorded submission before applying repetition rules. It uses gwflow's
assessment of scheduler information and checked execution evidence.

For example, a completed computation job that must run again after a Task change
has `state: "pending"` and `observed_state: "completed"`. Its recorded submission
identifiers still refer to the earlier job. That earlier completion no longer
counts toward the required work.

Use `null` for `observed_state` when no recorded submission or observation is
available. Use the recognized value `unknown` when a submission was inspected
but its outcome remains unresolved. This distinguishes unavailable observations
from inspected submissions whose outcome is uncertain.

Use the existing status vocabulary for the initial JSON state values:

| Field | Values |
| --- | --- |
| Task `state` | `pending`, `queued`, `preparing`, `running`, `finishing`, `reusable`, `repairable`, `deferred`, `failed`, `canceled`, `blocked` |
| Job `state` and `observed_state` | `pending`, `queued`, `running`, `completed`, `failed`, `canceled`, `unknown` |

Task and job `state` are strings. `observed_state` can also be `null` under
the availability rules above. Use `canceled` in output while continuing to
accept `cancelled` as a status-filter alias. The compatibility rules below
allow future state additions.

Each Task summary includes a `progress` object with `completed` and `total`,
even without `--details`. Use ordinary status progress counts: computation
targets plus preparation and completion jobs, with the same rules for work
that must be repeated. `completed` is an integer or `null` when unavailable;
`total` is the integer job count for the current Task definition. Keep progress
separate from Task state. Blocked or deferred Tasks can have full counts, so
progress alone does not establish Reuse.

Keep requested fields present with JSON `null` when their value is unavailable.
For example, a job record includes a null submission identifier until one is
available. Omit optional sections when they are not requested, including job
records when `--details` is absent. This distinguishes unavailable values from
details the caller did not request.

Each Task reports its current condition in `state` and the planner's Next action
in a separate `next_action` field. The Next action describes planned treatment
within the inspected Execution scope. It is not a guarantee of submission:
blockers within that scope can prevent submission, and a later run replans and
rechecks prerequisites.

Use these lowercase `next_action` values, following the existing public
Next action categories:

| Value | Meaning |
| --- | --- |
| `run` | Start a new attempt or resume its unfinished initialization. |
| `continue` | Submit remaining pending jobs in the current attempt. |
| `retry` | Repeat steps that need another try in the current attempt. |
| `finish` | Finish pending completion work. |
| `repair` | Restore retained results from valid existing work. |
| `reuse` | Use valid retained results. |
| `wait` | Wait for work already queued or running. |
| `defer` | Revisit the Task in a later run after upstream result recovery. |
| `blocked` | Current conditions block new submissions. |

New Next action values are compatible additions within a schema version,
including during stable 1.x. Preserve existing values and their meanings.
Readers must keep unfamiliar values visible and treat their meaning as unknown,
without interpreting them as a known action. Removing or renaming existing
values, or changing their meanings, is a breaking change.

Each Task summary includes a `reason` object with string fields `code` and
`message`. The stable code and readable message explain the Next action.
Use a small, documented set of operational categories so scripts can distinguish
conditions without parsing prose. The codes do not expose every internal
diagnostic distinction.

When `next_action` is `blocked`, use these reason codes:

| Code | Meaning |
| --- | --- |
| `image_unavailable` | A required container image cannot be acquired or checked. |
| `input_unavailable` | A required external input is missing or cannot be checked. |
| `submission_unresolved` | A submission's outcome or identity cannot be established. |
| `activity_conflict` | Known queued or running work prevents the planned change. |
| `validation_failed` | Another validation problem prevents proceeding. |

The readable message supplies the specific details, such as the missing file
or conflicting job. Use `validation_failed` for remaining validation checks
without assigning a separate code to every internal failure.

For other Next actions, use these reason codes:

| Next action | Code | Meaning |
| --- | --- | --- |
| `run` | `no_attempt` | No recorded Task attempt exists. |
| `run` | `definition_requires_refresh` | The definition, commands, or required command records require a fresh attempt. |
| `run` | `inputs_changed` | Tracked external-input properties changed. |
| `run` | `upstream_attempt_changed` | An expected upstream attempt changed or will be replaced. |
| `run` | `prior_work_unusable` | Results cannot be accepted and existing work cannot restore them. |
| `run` | `initialization_incomplete` | Resume unfinished initialization of an already selected attempt. |
| `continue` | `submissions_pending` | Submit remaining jobs without repeating admitted work. |
| `retry` | `jobs_require_retry` | Repeat failed, canceled, or invalid steps, including interrupted preparation. |
| `finish` | `completion_pending` | Finish result installation, recovery, or required record compaction. |
| `repair` | `results_need_repair` | Restore damaged or missing retained results from valid work. |
| `reuse` | `results_valid` | Existing results and completion evidence satisfy ordinary Reuse checks. |
| `wait` | `jobs_active` | Existing jobs are queued or running. |
| `defer` | `upstream_recovery_pending` | Upstream results need recovery or checking before reassessing this Task's inputs. |

Resuming unfinished initialization preserves the already selected attempt ID.
The `run` action therefore does not always allocate a new ID. Keep specific
files, jobs, and recovery details in the readable message.

Include a separate list of relevant workflow notices in the JSON report. Each
notice contains string fields `code` and `message`, and `task_names`, a list
of strings identifying the affected Tasks. The code is stable and the message
is readable.
Keep required-action notices visible even when a state filter hides those Tasks
or leaves the Task list empty. Use ordinary status notice scope, including
relevant upstream Tasks without adding unrelated runtime blockers. This lets
scripts detect required actions without parsing prose or losing them to filters.

Use these workflow notice codes under the existing notice conditions:

| Code | Meaning |
| --- | --- |
| `workflow_blocked` | Blocking Tasks prevent new submissions within the inspected Execution scope. |
| `upstream_recovery_pending` | Deferred Tasks need a later invocation after upstream results are recovered or checked. |
| `completion_followup_required` | Retrying Tasks need a later invocation after their previous queued completion job settles. |
| `submission_investigation_required` | Current recorded submission observations have unresolved outcomes that need investigation. |

Each notice identifies the relevant Tasks. For `workflow_blocked`, list the
blocking Tasks. A Task can appear in multiple notices. Ordinary queued or running
completion alone does not require a follow-up notice.

Investigation guidance must retain the requirement to establish whole-Task
inactivity before using `gwflow recover`, including computation, preparation,
and completion jobs. The notice does not establish that recovery is safe or
necessarily required. Cancellation requests alone do not establish inactivity.

Each report includes an integer `schema_version`, starting at `1`, independent
of the gwflow version. Adding optional fields is compatible within a schema
version, and readers must ignore fields they do not recognize. Removing fields,
changing their types, or changing their meanings requires a new schema version.
This lets reports grow without tying scripts to individual gwflow releases.

New reason and notice codes are compatible additions within a schema version,
including during stable 1.x. Existing codes retain their meanings. Document that
readers must handle unfamiliar codes with a generic fallback that keeps their
readable messages visible, rather than rejecting the report or silently dropping
an unfamiliar notice.

New Task and job state values are also compatible additions within a schema
version, including during stable 1.x. Preserve existing state values and their
meanings. Readers must keep unfamiliar values visible and treat them
conservatively as unknown, inferring neither success, failure, nor inactivity
from them. Unknown to a reader does not mean that gwflow lacks the state:
preserve the reported value. Older scripts can still read the report but may
need updating to understand a new state. Removing or renaming existing states,
or changing their meanings, is a breaking change.

New inspection-error codes are compatible additions within a schema version,
including during stable 1.x. Preserve existing codes and their meanings.
Readers must handle unfamiliar codes as generic inspection failures while
keeping the original code and readable message visible. The structured error
document and nonzero exit still indicate that inspection failed.

Once the JSON interface is part of stable 1.x, preserve backward compatibility
throughout that major series. Breaking changes require a future major gwflow
release as well as a new schema version. This applies the
[stable CLI compatibility promise](0010-preserve-public-interfaces-and-completed-work-through-1-x.md)
to JSON consumers; the [pre-1.0 policy](0001-pre-1-0-development.md) still applies
before stability.

When inspection fails with an error gwflow can catch, JSON mode emits a
structured error document on stdout and exits with a nonzero code. Diagnostic
text goes to stderr. A successful inspection that reports failed or blocked
Tasks remains successful; those Task conditions are not inspection errors.
This lets scripts distinguish failure to inspect from problems in observed work.

When `--json` is requested, invalid status options also emit a structured JSON
error on stdout and return exit code 2. This includes incompatible display
options and invalid `--status` values. Send readable diagnostics to stderr and
omit Task results. Scripts receive structured errors for invalid status options
as well as inspection failures.

Start with two `error.code` values. Use `usage_error` for invalid status
arguments or options and return exit code 2. Use `inspection_failed` for a
caught failure loading or inspecting the workflow that prevents a complete
report and return exit code 1. Keep the specific cause in `error.message`.
More specific error codes can be added under the compatibility rules above.
Successfully inspected failed or blocked Tasks still return exit code 0.

If inspection fails partway through, return the structured error without partial
Task results. Return either the complete requested status report or the error.
Withholding partial observations prevents an incomplete set of Task records from
being mistaken for the complete requested view.

Reserve stdout for one JSON document in JSON mode. Send messages printed while
loading the authored workflow to stderr, along with diagnostic output. This
keeps redirected reports parseable even when workflow code prints messages.

JSON mode changes the output format while using ordinary status inspection rules.
Missing registry images can be acquired for selected Tasks and their required
upstream Tasks, including otherwise reusable Tasks. Existing cached images remain
reusable without registry access under the existing image acquisition rules.

The following example illustrates the summary layout for
`gwflow status report --json`. The Task completed its three jobs previously,
but upstream results must be restored before its inputs can be reassessed.
The attempt ID and messages are illustrative.

```json
{
  "schema_version": 1,
  "tasks": [
    {
      "name": "report",
      "attempt_id": "1234567890abcdef1234567890abcdef",
      "state": "deferred",
      "next_action": "defer",
      "progress": {"completed": 3, "total": 3},
      "reason": {
        "code": "upstream_recovery_pending",
        "message": "Upstream results must be restored before inputs can be checked."
      }
    }
  ],
  "notices": [
    {
      "code": "upstream_recovery_pending",
      "message": "Run again after upstream result recovery.",
      "task_names": ["report"]
    }
  ]
}
```
