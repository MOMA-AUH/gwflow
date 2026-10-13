# Cancel admitted work without pausing Tasks

For v0.6.2, managed Cancellation extends the backend-only cancellation boundary
in [ADR 0007](0007-give-managed-workflows-a-dedicated-cli.md), requesting that
admitted computation and lifecycle jobs stop while preserving valid completed
work, retained outputs, and recovery evidence. A later ordinary run may retry
eligible work through the existing inactivity and recovery checks, avoiding a
separate pause/resume lifecycle. Issuing a cancellation request alone never
establishes job inactivity.

Cancellation requests address the selected Tasks. For v0.6.2, dependent-job
handling remains the backend's responsibility, without an explicit downstream
expansion option. This keeps the command focused on selecting work to stop;
backend observations still determine when affected jobs are inactive.

Cancellation addresses the admitted computation and lifecycle jobs of each
selected Task's current recorded attempt. v0.6.2 does not add selection of older
attempts: normal replacement already requires the preceding attempt to be
inactive, and querying older scheduler records can introduce irrelevant
uncertainty after those records expire. Earlier attempts remain available for
inspection through `logs --history`.

The command requires explicit Task selectors: bare `gwflow cancel` is rejected,
while `gwflow cancel '*'` selects all Tasks. This makes whole-workflow
cancellation deliberate.

Task selectors match names from both current declarations and recorded attempts.
Tasks removed from the workflow remain selectable and are marked as "no longer
declared", since removing a declaration does not stop its admitted jobs.

All Task selectors are validated before cancellation requests begin, following
the shared rule that every supplied selector must match. A declared Task without
recorded attempts matches successfully and reports no admitted work to cancel.

An explicit cancellation command sends requests without an interactive
confirmation. `--dry-run` previews the affected Tasks and jobs without issuing
cancellation requests, supporting both advance inspection and scripted use.

Cancellation continues with identifiable jobs when another job's identity
cannot be resolved or its backend rejects the request. The report lists failed
requests and unresolved jobs individually, and the command exits nonzero if
either occurs, even when other requests succeed. This permits useful progress
without reporting a partially addressed request as wholly successful.

The command returns after reporting backend request outcomes, without polling
for confirmed inactivity. Its report distinguishes cancellation requests from
confirmed stopped jobs and makes clear when jobs may still be stopping. v0.6.2
does not add waiting or timeout options; recovery and cleanup retain their
existing inactivity checks.
