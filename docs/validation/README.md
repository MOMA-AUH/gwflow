# Validation

Run checks for the current checkout using the procedures below. The feature
directories preserve historical acceptance evidence, including tested versions,
source identifiers, commands, outcomes, and deployment limits. Their results
apply to the recorded implementation and environment; release numbers and issue
numbers identify that context inside each record.

The [v0.8.0 candidate record](v0.8.0/acceptance.md) ties the combined regression
journeys and interactive measurements to the installed release candidate and
documents the separate publication verification procedure.

## Local checks

Run from the repository root in a Python 3.12 environment. Installing the project
selects the pinned gwf 2.1.1 dependency and the current Rich requirement from
`pyproject.toml`. Install both example packages for the packaged-workflow tests:

```sh
python -m pip install .
python -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
python -m unittest discover -s tests -v
python tests/release_smoke.py
python -m compileall -q src tests examples
```

Container tests explicitly skip when their SIF fixtures are unset. Separate
filesystem tests use `/dev/shm` when available and report a skip if no writable
second filesystem is available. Review skips when recording validation scope;
they do not establish runtime or filesystem evidence.

## Container setup

Use Linux and deployment-provided Apptainer 1.5.4 on `PATH`. Prepare all three
fixtures before running the full suite with containers or the acceptance runner:

```sh
mkdir -p build/validation
apptainer build build/validation/basic.sif tests/fixtures/container.def
apptainer build build/validation/summary.sif examples/packaged/images/summary.def
apptainer build build/validation/report.sif examples/packaged/images/report.def
export GWFLOW_TEST_SIF="$PWD/build/validation/basic.sif"
export GWFLOW_TEST_SUMMARY_SIF="$PWD/build/validation/summary.sif"
export GWFLOW_TEST_REPORT_SIF="$PWD/build/validation/report.sif"
```

Fixture builds require access to the base image registry. Set but unusable
fixtures fail validation. Rerun the local checks above after fixture setup to
include real container execution. The [packaged example](../../examples/packaged/README.md)
describes the image contents and producer-cleanup-consumer scenario.

## Live backend acceptance

The [acceptance runner](../../tests/validate_containers.py) exercises installed
public CLI behavior with real local workers or Slurm. Install the project and
example packages and prepare all three images first, including for focused
registry validation. Choose a new evidence root for every invocation:

```sh
python tests/validate_containers.py --backend local --root build/validation/local-acceptance
python tests/validate_containers.py --backend slurm --queue short --root build/validation/slurm-acceptance
```

For Slurm, select an available site partition with `--queue`; `short` is the
example above. The runner uses the deployment's `sbatch` and `sacct`, requesting
one core, 256 MB, and two minutes per job. The installed environment, workflow,
evidence root, managed storage, and SIF files must be visible at the same absolute
paths on frontend and compute nodes. Registry acceptance also requires matching
CPU architectures and frontend registry access.

The default command/wait timeout is 600 seconds; change it with `--timeout` for
the deployment. A failure retains evidence and does not automatically cancel
submitted jobs. Inspect outstanding jobs before rerunning. Missing prerequisites
and skipped cases fail the acceptance runner.

Use `--case` for focused acceptance; omitting it runs every case. For registry
images, run the deterministic tests and the real backend cases:

```sh
python -m unittest discover -s tests -p 'test_registry*.py' -v
python tests/validate_containers.py --backend local --case registry --root build/validation/registry-local
python tests/validate_containers.py --backend slurm --queue short --case registry --root build/validation/registry-slurm
```

The registry case creates an isolated image cache under its evidence root and
acquires its image on the frontend. It checks cold acquisition, warm execution,
cache preservation, and recovery after an expected worker Input-baseline refusal.
Other container cases use the prepared fixtures.

## Planning performance checks

These tests use controlled workloads and observe the public CLI. They require
the installed project and local workers; their image observations use fixture
executables and do not require a live registry or Slurm:

```sh
python -m unittest discover -s tests -p 'test_planning*.py' -v
```

The [combined acceptance record](planning-performance/acceptance.json) preserves
the historical baseline comparison commands, workload, counts, and timing limits.
Running the current tests checks the installed implementation; it does not
reproduce old measurements automatically. Instrumented operation counts and
uninstrumented command times measure different things.

## Historical evidence index

The [installed compatibility baseline](compatibility/README.md) describes the
two-environment completed, cleaned, and incomplete Task journeys, the pinned bootstrap source,
runtime provenance, and how to test a later installed candidate against it.
The current v0.8.0 release work retains these journeys as regression checks.
Its same-source bootstrap is not evidence for all future upgrades, and v0.8.0
does not establish the future v1.0.0/1.x compatibility baseline.

The release column identifies the release associated with the feature. Tested
package metadata can precede that release; for example, the registry runs used
gwflow 0.4.1 and the planning measurements used 0.5.0. Follow each record for its
exact environments and limitations. Dates for planning evidence are its recording
dates from Git history; those records do not state separate measurement dates.

| Scope and records | Tested source identifier | Date | Associated release |
| --- | --- | --- | --- |
| Managed lifecycle: [acceptance](managed-lifecycle/acceptance.md) | gwflow 0.3.0 package; exact tested commit unrecorded | 2026-10-01 | v0.3.0 |
| Containers: [acceptance](containers/acceptance.md), [runtime observations](containers/runtime.json) | Runtime source `ec06a4f`; subsequent acceptance and correction revisions are recorded separately | 2026-10-01 | v0.4.0 |
| BeeGFS identity correction: [observations](containers/beegfs-identity.json), [explanation](containers/acceptance.md#beegfs-directory-identity-correction) | Source SHA256 in record, with base `f02bf86` | 2026-10-01 | v0.4.0 |
| Registry images: [acceptance](registry-images/acceptance.md), [runtime observations](registry-images/runtime.json) | Production base `587a49d`; acceptance implementation `86800de` | 2026-10-04 | v0.4.2 |
| Planning performance: [combined acceptance](planning-performance/acceptance.json) | Optimized production source `4d6cec1`; baseline and intermediate revisions in record | 2026-10-07 | v0.5.1 |

The intermediate planning records retain the individual changes' measurements
and regressions referenced by the combined acceptance record:

| Scope and record | Issue | Recording date |
| --- | --- | --- |
| [Task-job observations](planning-performance/task-job-observations.json) | [#146](https://github.com/MOMA-AUH/gwflow/issues/146) | 2026-10-06 |
| [Producer evidence](planning-performance/producer-evidence.json) | [#147](https://github.com/MOMA-AUH/gwflow/issues/147) | 2026-10-07 |
| [Shared inputs and images](planning-performance/shared-inputs-and-images.json) | [#148](https://github.com/MOMA-AUH/gwflow/issues/148) | 2026-10-07 |
| [Directory reads](planning-performance/directory-reads.json) | [#149](https://github.com/MOMA-AUH/gwflow/issues/149) | 2026-10-07 |

## Recording evidence

The interactive log resource tests also require a Linux `/proc` filesystem and
a C compiler named `cc`. Their test-only libc probe counts bytes returned by real
file reads and can hold a selected read to verify terminal responsiveness and
reader cleanup. It is loaded only in the test subprocesses and does not replace
gwflow modules or file contents. Captured-data paging checks measure retained
text and reconstruct a line larger than the window across UTF-8 boundaries;
installed-terminal checks cover streams, history, refresh, and cancellation of
blocked reads. These checks use no production cluster access.

The v0.8.0 interactive status work has [refresh/filter acceptance](status-browser/acceptance.json)
and [captured browser measurements](status-browser/measurements.json). These record
implementation checks for issue #229; release preparation and publication remain
separate work.

The [live-follow measurements](status-browser/follow-measurements.json) record
issue #232's actual log-read bytes and resource checks through rapid appends,
replacement, incomplete UTF-8, and repeated follow/pause/close cycles. The record
identifies the reviewed source commit and package versions; package metadata is
still 0.7.0 until release preparation. Reproduce it in an installed candidate
environment with:

```sh
python tests/benchmark_status_follow.py --output build/validation/status-follow/measurements.json
```

The follow and installed-terminal regression files separately cover missing and
unreadable logs, pending retries, scrolling, refresh, exact-submission pinning,
and blocked-reader cleanup. Both run in the ordinary full test suite. These
implementation checks do not substitute for release-artifact validation.

Keep raw logs, generated workflows, and machine-specific transcripts under the
ignored `build/validation/` directory. The acceptance runner writes environment
and summary JSON plus per-case command transcripts there. Commit selected
evidence under the relevant feature directory, preserving historical observations
when adding later results. Identify each record's scope, date, tested source,
versions, and associated release or issues.

Follow the [validation evidence convention](../../AGENTS.md#validation-evidence):
normalize paths with documented placeholders, preserve commands, results and
relevant device/inode observations, and verify JSON syntax and path normalization
against the raw evidence. Tests and [CI](../../.github/workflows/ci.yml) provide
ongoing checks; historical records preserve the outcomes of particular runs.
See [releasing](../releasing.md) for artifact and publication checks.
