# Provide explicit operator recovery for unresolved submissions

For v0.8.0, provide a supported, recorded operation through which an operator can
resolve an otherwise unresolved submission after independently establishing that
the affected jobs cannot still run. This extends the current contract, which has
no uncertainty override, because indefinite blockage without an operational
recovery path is unsuitable for the intended production workflows.

The operation must make the operator's responsibility explicit and preserve the
submission and recovery evidence. Ordinary `run --force` continues to respect
unresolved activity; requesting cancellation alone still does not establish
inactivity, consistent with
[ADR 0009](0009-cancel-admitted-work-without-pausing-tasks.md).

Resolving submission uncertainty preserves valid partial work. A subsequent
ordinary `gwflow run TASK` uses existing retry and recovery checks to retain
verified successful computation and execute or recover what remains. The
operator establishes inactivity, not successful computation: resolving a
submission must not turn unchecked outputs into successful execution evidence or
require a fresh Task attempt solely because uncertainty was resolved.

The interface is `gwflow recover TASK`, with no additional arguments or flags.
It records the resolution without submitting jobs; the user can then inspect
`gwflow run TASK --dry-run` and execute an ordinary run. Do not require an
assertion flag or a user-supplied explanation argument. Preserve the relevant
submission evidence and record the recovery operation automatically.

The command acts without an interactive confirmation: invoking it is the
operator's assertion that the whole Task is inactive, including when
machine-verifiable evidence is unavailable. It must refuse recovery when current
observations show any computation or lifecycle job in the Task queued or running,
even if that job is unrelated to the unresolved submission. This whole-Task
boundary keeps recovery simple while preserving verified successful work. The
command reports exactly which submissions it resolved. Its documentation and
blocked-work guidance must explain this responsibility; recovery does not cancel
jobs or establish successful computation.

Recovery addresses all unresolved submissions belonging to the Task's current
recorded attempt and leaves historical attempts unchanged, matching the current
attempt scope of Cancellation. Normal attempt replacement already requires the
previous attempt to be inactive; recovery does not introduce historical attempt
selection.
