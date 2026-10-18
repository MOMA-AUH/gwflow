# Use explicit force for untracked computation changes

For v0.8.0, users request fresh computation with `gwflow run TASK --force` when
package, tool, parameter, or environment changes have no effect on tracked
declarations, commands, image dependencies, or file observations. Do not add a
Task Reuse key: the existing explicit operation keeps the authoring API smaller,
accepting that operators must identify and force affected Tasks after untracked
changes. Command tracking remains mandatory as decided in
[ADR 0011](0011-track-command-changes-by-default.md).

Cleanup retains its existing meaning: `gwflow clean TASK --delete` removes
eligible disposable work while preserving retained results and Reuse evidence.
Force retains its activity and ownership checks, removes the selected Task's
previous results before replacement submission, and does not automatically select
downstream consumers. Consumers recognize a changed producer attempt when later
considered for execution. Package versions do not independently invalidate Reuse,
consistent with [ADR 0006](0006-keep-factory-provenance-separate-from-reuse.md).
