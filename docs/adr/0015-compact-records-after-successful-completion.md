# Require automatic lossless record compaction at successful completion

The first record-compaction feature reduces the metadata files retained after
successful Task Completion, accepting that running and failed Tasks still retain
individual records. Logs, computation files, and reducing metadata writes during
execution are outside this feature's scope, so the initial work addresses
accumulated metadata without also redesigning active execution.

Compaction preserves all existing evidence losslessly, including failed and
superseded submissions and operator recovery records within the compacted
attempt. Existing evidence checks and submission and recovery history remain
available with their existing meaning; successful Completion does not justify
discarding earlier evidence.

Compaction is required for completion-job success. Failure to remove redundant
original metadata fails the completion job even when the consolidated records,
retained outputs, and Completion evidence are already durable. Dependent jobs
remain blocked until retry succeeds; valid Completion evidence alone must not
bypass unfinished required compaction. This makes failure to reduce the metadata
footprint visible and actionable rather than allowing it to accumulate behind
maintenance notices.

A compaction failure preserves valid computation, retained outputs, and evidence.
An ordinary subsequent `gwflow run` resumes unfinished completion work without
repeating valid computation. Status must distinguish a compaction failure from
failed computation or invalid retained outputs. If inputs or outputs have changed,
the usual validity checks still apply.

Compaction runs automatically in the completion job. There is no separate
maintenance command for already-completed attempts and no migration or legacy
record support solely for backward compatibility. This follows
[ADR 0001](0001-pre-1-0-development.md): before v1.0.0, assume no production
users and choose the simplest coherent design. Recovery from interrupted
automatic compaction remains required through ordinary `gwflow run`; it is
part of the new behavior, not a compatibility feature.

Work cleanup preserves compacted storage. It updates compacted records directly
to record cleanup intent and outcome, retaining the existing ownership checks
and interruption-recovery guarantees without restoring the individual metadata
files. An interrupted or resumed cleanup must not undo the reduction in retained
metadata files.

Repair may temporarily restore individual metadata records, with automatic
compaction on successful completion. Interrupted or failed Repair may leave
individual records until retry succeeds. The transition back to individual
records must itself survive interruption without losing evidence or repeating
valid computation.

Successful compaction leaves a small, fixed upper bound on metadata files and
directories per attempt, independent of the number of computation targets or
submissions. Preserved history may increase their size, but not their count.
The implementation must establish and test a concrete bound, including removal
of redundant empty metadata directories. Logs and computation files are excluded
from this bound.

The storage protocol must preserve evidence during concurrent reads and writes
and interrupted transitions. In particular, submission acknowledgements can
arrive after worker execution, and other workers can read producer records while
compaction proceeds. The completion job cannot be assumed to be the last writer.
The specification must define authoritative storage and publication durability,
reader and writer coordination, and recovery from each interrupted transition;
the physical format and synchronization mechanism are not selected by this ADR.
