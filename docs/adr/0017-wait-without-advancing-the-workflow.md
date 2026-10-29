# Wait for completion without advancing the workflow

Provide `gwflow wait` to observe existing work without advancing the workflow.
The command submits no jobs and performs no automatic retries or recovery.
When completion requires another command, it reports that condition and returns
a nonzero exit code. This keeps waiting predictable for scripts and operators,
including when deferred Tasks need a later workflow invocation.

Use the same Task-selection rules and Execution scope as `gwflow run`. Accept
exact Task names and quoted Task-name patterns; no selectors means all declared
Tasks. Observe the selected Tasks and their required upstream Tasks without
adding downstream consumers or unrelated Tasks to the awaited scope. An observed
failure in required upstream work therefore ends the wait promptly too.

Fix the identities of the awaited Task attempts when waiting starts. If another
invocation replaces an awaited attempt, report the replacement and return a
nonzero exit code. This keeps the result tied to the work originally awaited.

Keep the awaited Task set fixed, but inspect the current workflow definitions
and inputs at each check using ordinary validity rules. If a tracked change
invalidates an awaited attempt, return a nonzero exit code at the inspection
that detects it, even while its jobs remain active. Removing an awaited Task
also ends the wait with an error. Leave existing jobs running. Unrelated edits
or newly declared Tasks do not expand the awaited scope. This prevents success
for obsolete work without silently switching the Tasks or attempts being awaited.

Return a nonzero exit code as soon as an awaited Task failure is observed, even
when other jobs remain queued or running. Leave those jobs running and make
their possible continued activity explicit in the report. A nonzero exit means
the requested completion was not achieved. It does not establish job inactivity.
This avoids waiting indefinitely for jobs blocked by failed dependencies.

Inspect immediately when waiting starts, then pause for 60 seconds between
inspections by default. Allow the pause to be changed with `--interval SECONDS`.
The one-minute default limits scheduler traffic during long waits while letting
already completed work return promptly after the first inspection.

Print a short starting summary and a final result. Between them, report changes
in Task states, relevant reasons, and scheduler availability, including recovery
after a scheduler problem. Keep unchanged checks silent while continuing to poll
at the configured interval. Suppress repeated identical scheduler errors. This
keeps long waits readable and batch logs compact.

During waiting, send all messages to stderr, including progress, diagnostics,
and the final result. Keep stdout empty. Redirect messages printed while loading
the authored workflow to stderr as well. Scripts use the exit code to determine
the outcome and can capture the messages together with `gwflow wait 2>wait.log`.

Waiting has no time limit by default because cluster queue times and computation
durations vary. Allow an explicit limit with `--timeout SECONDS`. Count elapsed
time from command startup, including workflow loading, waiting for locks,
scheduler queries, other inspection work, and pauses between checks. When the
limit expires, interrupt any ongoing inspection, clean up, and return exit code
124. Stop only the waiting command and its inspection processes; submitted
jobs continue running. The deadline applies during inspections, not only between
them.

Pressing Ctrl+C stops the waiting command and any ongoing inspection, performs
cleanup, and returns exit code 130. Briefly report that waiting was interrupted
and submitted jobs may still be queued or running. Do not cancel those jobs.

If a scheduler query fails, report that current job states are unavailable and
retry at the normal polling interval. A failed query establishes neither Task
success nor Task failure. Any configured timeout still applies. Without a
timeout, these retries can continue indefinitely, allowing temporary scheduler
outages to pass without ending the wait.

Also keep polling when a successful scheduler query leaves a submitted job's
outcome unknown. Report the uncertainty and use the normal interval and any
configured timeout while awaiting sufficient scheduler or checked execution
evidence. Without a timeout, this wait can continue indefinitely. Missing
scheduler information alone establishes neither completion nor inactivity and
does not prove that an operator command is required. Known failures and
invalidated attempts still end the wait under the rules above.

Other inspection errors end the wait with a nonzero exit code and a specific
error report. Examples include an invalid workflow file, unreadable Task records,
and conflicting saved and tracked job IDs. Leave submitted jobs running. Only
scheduler-query failures and insufficient job-outcome evidence receive the
automatic retries described above. Even a temporary filesystem or workflow-loading
error ends this invocation; the operator can restart waiting after it clears.

Return success only when every awaited Task attempt is reusable under ordinary
gwflow checks and its completion job succeeds, including record compaction.
Successful computation alone is insufficient while result installation or
record compaction remains pending. Retained results must be valid. Tasks that
already meet these conditions succeed on the first inspection.

Use the following exit codes so scripts can distinguish timeout and interruption
without parsing messages. For other unsuccessful waits, the error message
explains the specific cause.

| Code | Meaning |
| --- | --- |
| `0` | All awaited Tasks meet the completion conditions above. |
| `1` | Task failure or another wait or inspection error. |
| `2` | Invalid command-line usage. |
| `124` | The configured timeout expired. |
| `130` | Waiting was interrupted with Ctrl+C. |
