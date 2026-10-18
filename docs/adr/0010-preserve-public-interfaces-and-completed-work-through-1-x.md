# Preserve public interfaces and recoverable work throughout 1.x

The maintainer retargeted the current release work to v0.8.0 on 2026-10-18.
That release remains under the pre-1.0 policy in ADR 0001. The compatibility
guarantee below applies to the future v1.0.0 baseline and 1.x releases; v0.8.0
does not establish that baseline. Existing compatibility journeys remain useful
regression checks and preparation for the future stable release.

Starting with v1.0.0, gwflow preserves its public authoring API and CLI and
supports completed Task records across forward upgrades within 1.x: an unchanged
workflow can still load and reuse valid retained results after its internal
intermediates have been cleaned, without upgrade-induced recomputation or manual
record conversion. This accepts compatibility constraints on future changes
because preserving completed work is central to gwflow's value; v1.0.0 is the
baseline, without a promise to support every pre-1.0 record format, consistent
with [ADR 0001](0001-pre-1-0-development.md).

When all jobs are confirmed inactive before an upgrade, this guarantee also
covers valid incomplete Task attempts: retries preserve successful computation,
and interrupted result installation can recover without repeating valid
computation. An upgrade alone must not require a fresh attempt or manual record
conversion. This extends compatibility validation to incomplete attempts because
their valid partial work can also be expensive to reproduce.

Upgrades require all jobs of the affected workflows, including queued computation
and lifecycle jobs, to be confirmed inactive before changing their environment.
Users must finish or cancel those jobs and confirm they have stopped; a
cancellation request alone does not establish inactivity. Live upgrades and
mixed-version frontend/worker operation are outside the supported contract,
avoiding concurrent execution and record writes by different gwflow versions.
