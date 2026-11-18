# CI shard count measurements

On November 18, 2026, issue [#407](https://github.com/MOMA-AUH/gwflow/issues/407)
compared two and four shards of the full qualification workload. Both runs used
source `ad55248131be0e5f9a0344e6662d0ed0b160d4cb` and gwflow 0.11.2. A shard runs
one deterministic part of the discovered test suite.

The [measurement record](measurements.json) contains normalized commands,
versions, runtime identities, artifact inventories, and every job's timing.
Paths in the record are relative to the repository. `<REPO>` means a checkout
root. Raw machine logs and complete per-test records remain ignored under
`build/validation/fix-issues/20261118-ci-efficiency/`.

## Full workload comparison

Each manual CI run included package building, source installation, routine
compatibility, shared fixtures, exact runtime restoration, and real container
acceptance. Both also ran full release-process simulations and bundle checks.

| Shards | Sample count | Allocated runner minutes | Elapsed minutes | Tests | Test skips or failures |
| --- | --- | --- | --- | --- | --- |
| [Two](https://github.com/MOMA-AUH/gwflow/actions/runs/44371388219) | 1 | 68.65 | 39.05 | 1126 | 0 |
| [Four](https://github.com/MOMA-AUH/gwflow/actions/runs/44371404467) | 1 | 88.48 | 30.03 | 1126 | 0 |

Allocated runner time sums complete job durations. It includes setup, transfers,
test execution, cleanup, aggregation, and required result checks. Elapsed time
measures the first job start through the last job completion, including waits
between dependent jobs. The record lists initial dispatch queue time separately.
GitHub reported zero billable time for these hosted runs. That billing value is
different from the allocation measure above.

Every discovered test ran exactly once across the expected shards in each run.
The coverage gate checked the exact candidate package, dependency manifest,
source, and fixture identities. Both runs used an identical dependency manifest.
Their independent builds produced different candidate package checksums. Each
run consistently tested its own exact artifact.

## Decision and limits

Choose two shards for the CI cost goal. This sample used 19.83 fewer allocated
runner minutes, with 9.02 minutes more elapsed feedback time. Its 39-minute
feedback time was near an earlier 38.4-minute full pipeline in this issue queue.
This choice accepts that delay in exchange for fewer repeated setup jobs and
the lower observed allocation cost. It does not establish a feedback SLA.

Keep the existing deterministic assignment by test identity. Timing-aware
redistribution can shorten the longest shard, but it does not remove test work.
The historical timing observations suggested only a few minutes of benefit.
That benefit does not justify a maintained timing baseline in this change.
Each shard keeps its assignment when retried. Missing, duplicate, failed,
skipped, or mismatched records still fail the complete coverage gate.

These are single observations, not a controlled estimate of savings. The runs
used the same Ubuntu image and runner version on independent hosted machines.
Sampled regions differed, and setup, network, and host performance varied.
Per-test execution totaled 3076 seconds with two shards and 3627 with four.
That variation contributes to the cost difference and cannot be attributed to
shard count. Cache state and transfer delays also remain in the recorded totals.

The provisional pre-1.0 compatibility checks ran. Stable preserved-release
comparisons were inapplicable under the checkout-pinned descriptor. Both runs
used attempt 1 and required no retries. No required test coverage was skipped.
No release publication ran.

Use the [comparison commands](../README.md#compare-suite-shard-counts) to repeat
the experiment when the workload or runner environment changes. Compare both
counts at the same full source SHA and retain complete coverage evidence.
Do not treat these measurements as a general performance guarantee.
