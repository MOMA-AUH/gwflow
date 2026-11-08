# Adopt gwf 3 local schedulers per workflow directory

For the gwf 3 upgrade, gwflow adopts upstream's local scheduler selection by
resolved workflow directory. Each directory has its own Unix socket and its own
core and memory budgets. This replaces the earlier host/port configuration
preserved by [ADR 0007](0007-give-managed-workflows-a-dedicated-cli.md) and
[ADR 0019](0019-own-local-job-termination.md).

The earlier TCP connection allowed several workflow directories to share one
scheduler. Retaining that arrangement requires custom scheduler selection and
log ownership across directories. We accept separate schedulers to follow
upstream's design and reduce the integration that gwflow must maintain.
Schedulers account for declared core and memory requests. These reservations do
not enforce limits on a program's actual CPU or memory use.

Budgets are independent across workflow directories. Operators must divide the
available capacity when running several local schedulers on one machine. An
idle scheduler does not lend its capacity to another workflow's scheduler.

Local jobs use upstream's one-hour walltime when no explicit value is supplied.
This replaces the earlier absence of a local runtime limit and applies to
computation, preparation, and completion jobs. Workflow defaults, Task defaults,
target options, and the respective lifecycle defaults can set longer limits
for the jobs they govern. The release documentation must explain this change,
including its effect on long preparation and result-transfer operations.

gwflow retains ADR 0019's ownership of job descendants, exact Submission
identity checks, and durable termination evidence. A terminal observation must
still establish that owned computation stopped. Unexpected supervisor loss
remains unresolved. Adopting upstream scheduling does not authorize replacing
these guarantees with upstream's process-group termination alone.

Local jobs continue writing stdout and stderr to files during execution, using
gwf 3's log paths. This preserves the live log viewing required by
[ADR 0014](0014-make-terminal-status-interactive-by-default.md) and avoids
buffering entire job logs in the worker's memory. Output becomes visible as
programs and the execution runner flush it.

gwflow supplies the managed execution behavior through its own scheduler
subclass. The gwf package remains unchanged. The adapter must preserve both
streaming and the termination guarantees above while using upstream scheduling.
This depends on internal scheduler methods rather than a stable extension
contract. Keep that dependency confined to the worker adapter, retain an exact
gwf version pin, and validate the integration for each dependency upgrade.

The first gwf 3 release adapts existing gwflow functionality. Optional target
isolation and selective-output controls are deferred. The upgrade follows
[ADR 0001](0001-pre-1-0-development.md): preserving earlier workflow state does
not justify compatibility shims, migrations, or parallel legacy behavior.
