# Validation

The [automatic compaction acceptance record](automatic-compaction/README.md) covers
installed workers, recovery, concurrent writes, and the retained metadata bound.

The [compaction scale and live Slurm record](compaction-scale/README.md) covers
metadata counts, planning I/O, cache behavior, and the shared-filesystem journey.

Run checks for the current checkout using the procedures below. The feature
directories preserve historical acceptance evidence, including tested versions,
source identifiers, commands, outcomes, and deployment limits. Their results
apply to the recorded implementation and environment; release numbers and issue
numbers identify that context inside each record.

The [v0.8.0 candidate record](v0.8.0/acceptance.md) ties the combined regression
journeys and interactive measurements to the installed release candidate and
documents the separate publication verification procedure.
The [v0.8.0 publication record](v0.8.0/publication.md) identifies the published
artifact and records its successful channel installation and smoke checks.

## Maintainer-led HPC testing

The maintainer routinely tests gwflow on a real Slurm HPC cluster.
This remains maintainer-led validation alongside automated local, container,
and controlled-Slurm checks. The maintainer confirmed this established practice
on November 10, 2026, as recorded in
[#363](https://github.com/MOMA-AUH/gwflow/issues/363).

Use this context when planning validation or reviewing readiness.
The absence of a checked-in or agent-run live-cluster record alone does not
create a release blocker or justify proposing another qualification task.
Specific observed regressions or changed requirements can still warrant
targeted investigation. This guidance adds no HPC run, approval gate, or
release checklist requirement.

Individual CI and release records describe only the environments and checks
actually performed. Routine maintainer testing does not establish a passing
run for a particular commit or broaden the
[supported deployment profile](../operating.md#compatibility-boundary).
The [live backend prerequisites](#live-backend-acceptance), including identical
absolute paths on frontend and compute nodes, still apply.

## Pull request checks

CI compares the complete pull request with its base commit. It uses a lightweight
path only when every changed pathname belongs to this exact prose allowlist:

- `README.md`
- `CHANGELOG.md`
- `docs/authoring.md`
- `docs/operating.md`
- `docs/reference.md`
- `docs/history.md`

The lightweight path runs the recommended README installation check and the CI
classification and result checks. It also checks the complete diff for whitespace
errors. It does not build packages or execute containers.

Unknown paths select full qualification. Source, tests, package configuration,
workflows, actions, image inputs, compatibility records, ADRs, and agent guidance
therefore select full qualification. A change to both prose and any such path
also selects full qualification. Renames check both the old and new pathname.
Executable files and symbolic links do not qualify as prose.

Both required checks, `tests` and `conda`, run for either path. They require
successful classification and every selected check. A failed, cancelled, or
unexpectedly skipped selected job fails both checks. If CI cannot compare the
complete diff, classification fails instead of accepting a prose-only result.

Explicit release qualification always runs the complete pipeline. It does not
use the pull request classification.

### Installed test suite

CI runs the complete discovered installed-package test suite in one job.
The coverage gate requires every discovered test exactly once, the same runtime
identity, and no skipped tests. Per-test timings remain in the `conda-suite`
artifact. If the suite fails, rerun its job and the aggregate through GitHub
Actions while the transfer artifacts remain available.

Manual dispatch of `ci.yml` selects the full pipeline and all release-process
simulations. It does not authorize release publication.

The [November 18, 2026 comparison](ci-shards/README.md) preserves historical
measurements of the former two/four-shard configuration. Sharding and its
configuration options have been removed in favor of a single suite job.

### Release-process simulations

Release-process simulations test the machinery that preserves and publishes a
release. They differ from application regression tests and routine compatibility
comparisons against the checkout-pinned baselines from ADR 0018.

| Check | Prose-only PR | Unrelated application PR | Relevant or unknown PR | Explicit release qualification |
| --- | --- | --- | --- | --- |
| README installation and selection regressions | Run | Run | Run | Run |
| Full application suite and real container acceptance | Omit | Run | Run | Run |
| Routine checkout-pinned compatibility | Omit | Run | Run | Run |
| Fresh-cache qualification round trip | Omit | Omit | Run | Run |
| Controlled dependency replacement and fixture profiles | Omit | Omit | Run | Run |
| Qualified release bundle | Omit | Omit | Prepare | Prepare |

The complete PR diff also selects the release-process simulations. Only an
explicit set of presentation, browser, and terminal paths can omit them.
The classifier in `tests/ci_changes.py` records that set. Both paths of a rename
must qualify. Unknown paths and changes to release implementation, packaging,
dependency policy, fixtures, workflows, or qualification inputs run the simulations.
A failed comparison or invalid classification cannot select omission.

An unrelated application PR still runs every application test, real container
acceptance, and the applicable provisional or preserved-release compatibility
comparison. It produces no qualified release bundle. Explicit release
qualification always runs the simulations for its exact source and package,
regardless of the PR path selection.

### Artifact retention and reruns

Transfer artifacts carry files between jobs in the same workflow run.
`gwflow-container-fixtures`, `gwflow-ci-runtime`, and `gwflow-ci-channel` expire
after three days. The fixture policy applies to standalone cache preparation
and full qualification, which use the same upload step. Fixture caches remain
separate, with the exact input keys and branch access rules described below.
Consumers still require the recorded source and run identities and checksums.

Diagnostic artifacts retain their existing retention policy. These include
`installed-source-evidence`, `conda-suite`, `container-acceptance-evidence`,
`conda-runtime-evidence`, and `gwflow-publication-evidence`.
The qualified `gwflow-conda-release` bundle keeps its 90-day retention for
release qualification and publication. Published release attachments remain
subject to the [release preservation policy](../adr/0018-preserve-independent-compatibility-environments.md).
The three-day policy applies to new transfer uploads. It does not change the
expiry of existing artifacts or delete them early.

After a transfer artifact expires, rerunning only a failed consumer job cannot
download it. Rerun the complete workflow to recreate all transfers and their
matching identities. For a standalone fixture-cache run, dispatch that workflow
again on `master`. Available caches can reduce preparation time, but they do
not replace the required artifacts for that run.

## Local checks

Run from the repository root. Create the development environment with Python
3.12 and the official gwf 3.0.0 package from `gwforg` before installing with pip.
The package metadata requires exactly that gwf version. Install both Task fixture
packages for the packaged-workflow tests:

```sh
conda env create -f environment.yml
conda activate gwflow-dev
python -m pip install .
python -m pip install ./tests/fixtures/packaged_tasks/packages/summary-task ./tests/fixtures/packaged_tasks/packages/report-task
python -m unittest discover -s tests -v
python tests/release_smoke.py
python -m compileall -q src tests example
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
apptainer build build/validation/summary.sif tests/fixtures/packaged_tasks/images/summary.def
apptainer build build/validation/report.sif tests/fixtures/packaged_tasks/images/report.def
export GWFLOW_TEST_SIF="$PWD/build/validation/basic.sif"
export GWFLOW_TEST_SUMMARY_SIF="$PWD/build/validation/summary.sif"
export GWFLOW_TEST_REPORT_SIF="$PWD/build/validation/report.sif"
```

Fixture builds require access to the base image registry. Set but unusable
fixtures fail validation. Rerun the local checks above after fixture setup to
include real container execution. The
[packaged Task fixtures](../../tests/fixtures/packaged_tasks/README.md) retain
coverage for installed Task packages and the producer-cleanup-consumer scenario.

CI prepares one complete fixture set for each pipeline. The preparation job
can reuse a cache, a saved copy of unchanged images from an earlier run.
It accepts only an exact match for the runner platform, Apptainer version,
image definitions, copied sources, and build configuration. The definitions
pin the base image by its immutable digest. Changes to these inputs select
a new cache entry.

The preparation job compares every cached image with its recorded checksum.
It rebuilds incomplete or corrupt cache entries. It then uploads one artifact
with the current source and run identities.
Each test group and container acceptance job installs Apptainer, downloads
that artifact, and compares its identities and checksums before use.
Missing images or corrupt bytes in that artifact fail validation.
Failed preparation also fails validation.

The fixture artifact remains separate from the tested gwflow Conda package
and its runtime record. Container and bioinformatics tests remain enabled in
every full-suite group. CI retains preparation and consumer setup timings
with the fixture evidence to compare elapsed time and total runner time.

GitHub limits cache access by branch. A pull request can read its base branch
cache. A cache saved by a pull request belongs to that request's merge ref.
Other pull requests and the base branch cannot read it. See the
[GitHub cache access rules](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).
Do not assume that a successful pull request populates the shared base cache.

To populate the cache for other pull requests, run the fixture workflow on
`master` after the fixture changes merge:

```sh
gh workflow run fixture-cache.yml --ref master --repo MOMA-AUH/gwflow
```

This workflow prepares fixtures without release publication or the full suite.
The full qualification pipeline still runs every installed-package test and
real container acceptance when it reuses cached images. The fixture manifest
records whether preparation built or reused the images, with setup timings.

To refresh the base image, resolve the replacement digest from its registry.
Update all three definitions and run full qualification before merging.
The pinned build tools are in `tests/fixtures/image-build-requirements.txt`.
Changes to that file, the definitions, or copied sources change the cache key.
Changes to the fixture helper or Apptainer installation policy also invalidate it.
To discard an otherwise unchanged entry, delete its key from the repository's
Actions cache and rerun the fixture workflow. Cache misses rebuild all three
images. Unavailable cache storage does not remove the full build path.

## Bioinformatics example

The [public example](../../example/README.md) uses committed synthetic reads and
three public Bioconda images. Its ordinary test checks that the committed dataset
regenerates exactly. Real execution uses Apptainer and must be enabled explicitly.

From the repository root, run the tests with real container execution enabled:

```sh
GWFLOW_TEST_BIOINFORMATICS=1 python -m unittest discover -s tests -p test_example.py -v
```

The tests start local workers in a temporary directory and acquire missing images.
They compare the actual alignment statistics with the committed expectations and
inspect both samples in the combined report. They also clean the producer Tasks,
run the report from retained statistics, and establish that unchanged reruns
submit no new jobs.

Use `GWFLOW_IMAGE_CACHE` to select a different image cache if needed.
Keep `GWFLOW_TEST_BIOINFORMATICS=1` set when running the full suite to include this
integration test. An unset variable skips real bioinformatics execution and does
not establish container evidence.

## Live backend acceptance

The [acceptance runner](../../tests/validate_containers.py) exercises installed
public CLI behavior with real local workers or Slurm. Install the project and
Task fixture packages and prepare all three fixture images first.
Focused registry validation also requires these fixtures.
Choose a new evidence root for every invocation:

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
The current checkout retains these journeys against the pinned native gwf 3
bootstrap. They do not establish compatibility with earlier gwf 2 state or the
future v1.0.0/1.x compatibility baseline. Historical records keep their original
versions and results.

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
