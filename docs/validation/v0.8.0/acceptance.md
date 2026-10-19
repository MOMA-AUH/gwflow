# v0.8.0 candidate acceptance

This record covers release preparation for [#210](https://github.com/MOMA-AUH/gwflow/issues/210)
and [#217](https://github.com/MOMA-AUH/gwflow/issues/217), recorded on 2026-10-19.
It does not claim publication or establish the future v1.0.0 stable compatibility
baseline. [Publication #218](https://github.com/MOMA-AUH/gwflow/issues/218) requires
the successful tag workflow and a fresh channel installation of its tested artifact.

The mandatory-tracking, recovery, compatibility, and operating-guide work is
merged. [ADR 0014](../../adr/0014-make-terminal-status-interactive-by-default.md#implementation-delivery--2026-10-18)
records all ten merged presentation slices and their required CI results.

## Candidate and reproducibility

Package metadata and the Conda recipe identify `0.8.0`. Both example packages
accept this release. The local candidate wheel is
`gwflow-0.8.0-py3-none-any.whl`, SHA256
`16b4c058dd0c7b1ab4f1bb5e45277a7c03f0f9f4fc27d19c6dcdcd333d01efb6`.
Its installed module inventory and every module's contents were compared with
`src/gwflow/`. The wheel was built from a clean source copy to exclude obsolete
local build products. Final source and Conda artifact identities are also
available from the PR's required `tests` and `conda` checks.

Raw local logs stay under ignored `build/validation/v0.8.0/`. In commands below,
`<PYTHON>` means the Python 3.12 executable in the independent installed candidate
environment; all other relative paths start at the checkout root. The candidate
uses Python 3.12.14, gwf 2.1.1, Rich 15.0.0, and Textual 8.2.8. The runtime was
copied to local temporary storage to avoid shared-storage import latency, then
the candidate and both example wheels were installed into it.

```sh
<PYTHON> -m unittest discover -s tests -v
<PYTHON> tests/release_smoke.py
<PYTHON> -m compileall -q src tests examples
<PYTHON> -m mypy --follow-imports=silent --ignore-missing-imports tests/release_provenance.py
```

The clean candidate passed all three smoke journeys in 8.906 seconds and all
15 compatibility cases without skips. Compilation and the provenance script's
typecheck passed. Controlled CLI checks additionally accepted matching artifact
metadata and rejected a wrong SHA256, filename, and channel with exit status 2;
these fixtures are guard validation, not publication evidence. The PR checks
record the final full-suite and installed-Conda results before merge.

The release smoke runner preserves the packaged producer/cleanup/new-consumer
and troubleshooting/cancellation/history journeys. Its third journey establishes
unresolved submissions, records `recover`, previews, and retries through normal
commands. It checks no recovery submissions or cancellations, unchanged original
evidence and attempt identity, retained output contents, and execution counts
showing that successful sibling computation did not repeat.

The full installed suite exercises combined interactive/static behavior, real
keyboard input with both streams on a terminal, cached navigation, refresh and
failure recovery, history, bounded paging, live following, errors, terminal
restoration, and resource cleanup. The captured 100-Task/1,000-job cases and
existing real-inspection query/I/O bounds remain in both CI suites.

## Compatibility and measurements

[Compatibility evidence](compatibility.json) records all 15 completed and
partial-work journeys between bootstrap source
`8bad5986278152d613f356282e75fe9f65085a4c` (package metadata `0.7.0`) and the
independent `0.8.0` wheel above. It retains exact artifact identities, commands,
exit codes, runtime provenance, storage device/inode observations, and outcomes.
The bootstrap descriptor remains unchanged. All baseline jobs are confirmed
inactive before candidate workers start; storage stays in place. No manual
record conversion or future-release upgrade is implied.

[Browser measurements](browser-measurements.json) capture 12 interaction cycles
at 40x12, 80x24, and 120x40 with 100 Tasks and 1,000 jobs.
[Follow measurements](follow-measurements.json) capture real read counts and
resource checks under appends, replacement, UTF-8 boundaries, and view cycles.
Each identifies the machine, dependencies, source digests, command, and limits.
These automated measurements use no tight wall-clock gate and do not measure
real workflow imports, scheduler performance, or production-pipeline latency.
Median first/last-row interactions were 0.082–0.084 seconds; captured snapshot
replacement was 0.200–0.241 seconds across the three sizes. No old snapshots
remained, and sampled async-task and file-descriptor counts did not grow.
The live-follow workload passed its read and resource bounds in 19.844 seconds.

## CI and publication boundary

The required `tests` job installs the source and examples, runs the independent
compatibility matrix, full suite, all three release smoke journeys, and real
Apptainer/local acceptance. The `conda` job builds and installs the candidate in
a fresh environment, verifies its file-channel origin, and runs the full suite
and smoke journeys. Both must pass on the final PR head before squash merge.
The PR's artifacts preserve installed-source acceptance summaries and the exact
Conda candidate. Local test logs without SIF fixtures explicitly report skips;
the Conda suite likewise reports its container-runtime skips. The source CI job
supplies all three real SIF fixtures.

After merge, tag that protected-master commit `v0.8.0`. Follow
[the release procedure](../../releasing.md) to build and test the tagged artifact,
verify its checksum before upload, install it from MOMA-AUH in a fresh
environment, and run the three smoke journeys. The final provenance step matches
the installed package's filename, SHA256, version, and channel URL to the tested
artifact. Only after success create the GitHub release from the
[prepared notes](../../releases/v0.8.0.md), adding the run link and immutable
identifiers and attaching `publication.json` and `SHA256SUMS`.

CI uses hosted Linux workers and controlled replacement Slurm commands with no
production-cluster access. The [container](../containers/acceptance.md) and
[registry](../registry-images/acceptance.md) live Slurm observations are historical,
not v0.8.0 production acceptance. Representative live-cluster pipeline validation
remains follow-up work. No broader platform/backend support, relocation, live
upgrade, mixed-version execution, or general pre-1.0 compatibility is claimed.
