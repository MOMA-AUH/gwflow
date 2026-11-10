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

Updated workers advertise this guarantee and each full Submission name in the
existing state response. Frontends verify the immutable submission identity
before trusting a reused numeric job ID or issuing cancellation. Older workers
remain available for observation, but cannot accept new managed submissions or
verified cancellation requests. Their unsafe terminal observations cannot authorize retry,
replacement, or cleanup. An operator must establish inactivity before using the
existing [recovery operation](0013-provide-explicit-recovery-for-unresolved-submissions.md).
Slurm retains its existing cancellation behavior.

For the gwf 3 upgrade, [ADR 0020](0020-adopt-gwf-3-local-schedulers-per-workflow.md)
replaces the earlier worker configuration with a scheduler per workflow
directory. The descendant ownership and termination guarantees above remain
required.

On 2026-11-10, issue #362 replaced one termination file per Submission with a
single workflow ledger and a persistent writer lock. The ledger retains every
exact Submission name and state. History increases bytes, not file counts.
Quiescent storage uses two files in `.gwf/local-termination` and one directory.
The separate attempt bound remains two files and one directory.

Writers serialize complete ledger updates and sync an atomic replacement before
the scheduler exposes a terminal state. Readers use the complete previous or
new document without taking the writer lock. Writers never take attempt locks
or wait for jobs. The completion job's termination publication follows its exit,
so it updates the ledger independently of attempt compaction.

An interrupted replacement leaves the previous ledger authoritative. Missing,
malformed, or nonterminal evidence cannot establish inactivity. A later writer
or scheduler restart removes abandoned temporary files under the writer lock.
It never promotes a temporary file to evidence. Failed publication retains the
running job's resource reservation and unresolved state. Operator recovery
still requires independent evidence that computation stopped.

Under the pre-1.0 policy, this format does not migrate earlier loose receipts.
After stopping an earlier scheduler and establishing inactivity, operators use
the existing recovery command before reusing that work.
