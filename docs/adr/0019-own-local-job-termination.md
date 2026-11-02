# Own local job termination before reporting inactivity

On 2026-11-02, issue #307 required local Cancellation to stop computation
descendants before retry, replacement, or cleanup. gwflow owns the local job
lifecycle while preserving gwf's worker options, protocol, submission identities,
and backend configuration. This revises ADR 0007's unchanged-worker decision
because the pinned scheduler reports cancellation before stopping the full job.

Each job runs beneath a separate Linux supervisor that adopts and reaps its
descendants. A worker-owned pipe requests termination and also closes when the
worker exits. The scheduler publishes a terminal state only after the supervisor
confirms cleanup. Unexpected supervisor loss remains unresolved. This implements
[ADR 0009](0009-cancel-admitted-work-without-pausing-tasks.md): request acceptance
does not establish inactivity, and completed sibling work remains available.

Updated workers advertise this guarantee in the existing state response. Older
workers remain available for observation and cancellation, but cannot accept new
managed submissions. Their unsafe terminal observations cannot authorize retry,
replacement, or cleanup. An operator must establish inactivity before using the
existing [recovery operation](0013-provide-explicit-recovery-for-unresolved-submissions.md).
Slurm retains its existing cancellation behavior.
