# Earlier release notes

These notes preserve the release-specific descriptions formerly in the README.
Commands and compatibility statements here describe those versions, not the
current interface. For current usage, see the [quickstart](../README.md) and
[reference](reference.md). Historical validation remains linked to its recorded
version and environment.

## v0.5.3

The v0.5.3 release introduces decorated Task factories and stable names through
`@task_template` and `workflow.task(...)`, replacing the previous registration
method. Registrations can supply aliases and instance keys; module moves alone do not
invalidate Reuse. Packaged examples now require gwflow `>=0.5.3,<0.6`.

Managed status groups Task instances by their decorated factory and name prefix.
It shows reusable counts, state totals, bounded attention previews, and required
workflow notices. Select a group or use `--instances` for Task rows; select a
Task or public job, or use `--details`, to include its jobs. All views preserve
full identifiers and adapt to terminal width. Full reasons, attempts, paths,
and backend information remain available through `gwf explain --details`.

## v0.5.1

The v0.5.1 release reduces repeated planning reads for Task jobs, producer
completion evidence, shared inputs, and container images. Scoped directory
handles reduce repeated directory opens while preserving pathname validation.
These observations last for one planning pass; submission and workers still
perform fresh checks. The [planning validation record](validation/planning-performance/acceptance.json)
documents reduced reads and opens, lifecycle regression coverage, and timing
limits: measured command times remain broadly similar. Python and Conda packages
now both require Rich `>=15.0.0,<16`.

## v0.5.0

The v0.5.0 release adds a lifecycle-aware Task overview with progress that counts
preparation, computation, and completion. Status, explain, dry-run, and run share
static terminal presentation, complete Task details, and plain output for logs.
Explain, dry-run, and run show the same intended Task plan; actual runs also
report confirmed submissions, uncertain acceptance, and jobs not attempted.
Managed status filters now select whole Tasks by their primary state, and
`--format` modes are reserved for ordinary gwf workflows. See the
[v0.5.0 command guide](https://github.com/MOMA-AUH/gwflow/blob/v0.5.0/README.md)
for that release's selection, presentation controls, and recovery notices.

## v0.4.2

The v0.4.2 release adds explicit `docker://` registry images, acquired on the
frontend during planning and shared across a user's workflows. Missing images
are pulled into a shared cache with progress and failure reporting; cached
images are reused without registry access. Local and acquired images use the
ordinary Input baseline for execution, recovery, and Reuse. See the
[registry example](../examples/registry/workflow.py) and
[registry acceptance record](validation/registry-images/acceptance.md) for
recorded local/Slurm validation, and the
[validation guide](validation/README.md) for current checks.

## v0.4.1

The v0.4.1 release removes the ignored `Task(working_dir=...)` option and requires
the current inode-only storage identity records for managed operations. See the
[v0.4.1 compatibility and transition guidance](https://github.com/MOMA-AUH/gwflow/blob/v0.4.1/README.md)
for that release's upgrade constraints.

## v0.4.0

The v0.4.0 release adds per-target Apptainer images, declared-input staging with
custom names, and image-aware Reuse to the managed lifecycle. Tasks can combine
host and container commands while keeping named dependencies, partial retries,
fresh attempts, separate work/results filesystems, interrupted transfer recovery,
repair, and work cleanup. The
[packaged A/B/C example](../examples/packaged/README.md) demonstrates completing two
producers, cleaning their work, then computing a new consumer from retained
results with host or container execution. The
[v0.4.0 validation record](validation/containers/acceptance.md) maps that
release's acceptance matrix to tests and infrastructure observations, including
the BeeGFS storage correction; it is historical evidence, not a guarantee of
current compatibility.
