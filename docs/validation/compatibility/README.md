# Installed compatibility comparisons

[ADR 0010](../../adr/0010-preserve-public-interfaces-and-completed-work-through-1-x.md)
requires forward compatibility from the future v1.0.0 baseline for public
workflows and valid managed work. The project remains under the pre-1.0 policy.
These journeys test current behavior and prepare for the stable baseline.
[ADR 0018](../../adr/0018-preserve-independent-compatibility-environments.md)
requires preserved artifacts and independent runtimes for stable comparisons.
The candidate uses its own supported Python and dependency versions.

## Select pinned releases in CI

The [baseline descriptor](baseline.json) uses schema version 2. Its `stable`
field selects the comparison mode. Before the first stable publication,
`stable: null` selects the existing source comparison at `source.commit`.
The source pin must identify a complete Git commit. This provisional comparison
keeps every journey active without claiming an independent stable baseline.

After stable publication, `stable` contains `original`, `latest`, and an
`intermediate` array. Each release reference contains `version`, `source_commit`,
and `assets`. Versions use final tags from `v1.0.0` onward. Each asset maps its
exact filename to an object with its `url` and expected `sha256`.
Include exactly the Conda package, versioned validation archive, and outer
`SHA256SUMS` listed in
[the release procedure](../../releasing.md#stable-release-attachments).
The selector verifies the three pinned downloads and safely unpacks the archive
before restoring the runtime. The inner package and manifest checksums must agree
with the qualified release bundle.
The publication record must identify the same package and source.
Store these references in Git. Keep packages and downloaded manifests outside Git.

The routine matrix contains the original and latest releases. When both references
identify the same release, the tool runs it once and records both roles.
Conflicting identities for one version fail selection. An intermediate reference
also needs a nonempty `coverage` reason describing a distinct compatibility case.
Do not add every historical release. Review the reason with the descriptor change.

Run selection without downloading artifacts:

```sh
python tests/select_compatibility.py plan \
  --descriptor docs/validation/compatibility/baseline.json \
  --output build/validation/compatibility-plan.json
```

CI reads only the descriptor in its checkout. It never asks GitHub for a moving
latest release. After stable publication, the Conda job downloads each selected
bundle and checks every pinned checksum before restoration. It then invokes the
independent runner for all 15 journeys per selected baseline. The source job
runs the provisional comparison only while `stable` remains null.
The existing controlled runtime checks continue to test restoration before the
first stable publication. They do not create or publish releases.

To run the stable matrix, first install the candidate independently as described
below. Use the driver interpreter, candidate variables, and a new evidence root:

```sh
DRIVER_PYTHON tests/select_compatibility.py run \
  --descriptor docs/validation/compatibility/baseline.json \
  --candidate-python CANDIDATE_PYTHON --candidate-artifact CANDIDATE_ARTIFACT \
  --candidate-source CANDIDATE_SOURCE \
  --root build/validation/pinned-compatibility
```

Each selected release has separate evidence and failures identify its version.
CI retains the selection and normalized comparison records even when a check fails.
Raw downloads and logs remain under the ignored evidence root. A failed download,
restoration, or journey fails the matrix. It cannot produce a passing skipped case.

An old checkout retains its original selection when rerun. It does not gain
coverage of later releases. Exact reconstruction also depends on upstream
dependency availability, host compatibility, and the recorded tools still working.
Before preparing a new candidate, merge the descriptor update for the latest
successfully published stable release. The release procedure defines that review.
Do not change an old descriptor merely to make its rerun select newer releases.

## Compare independent runtimes

A runtime contains Python, installed packages, and native dependencies.
The [comparison tool](../../../tests/compare_runtime.py) restores the baseline
from a [Conda capture](../conda-runtime.md) into a new prefix with a fresh cache.
It uses the captured package and explicit dependency manifest without a solve.
It does not rebuild the baseline from source.

Install the candidate artifact in a separate Conda environment with its own
supported dependencies. Run the tool from a third environment with `gwf` and
`packaging` installed. Use a Conda version that supports explicit SHA-256 manifests.
The driver environment controls the tests. Its packages do not supply the
baseline or candidate runtime.

In this example, `BASELINE_CAPTURE` contains the preserved capture files.
`CANDIDATE_PYTHON` identifies the independently installed candidate interpreter.
`CANDIDATE_ARTIFACT` identifies its exact Conda package.
`CANDIDATE_SOURCE` identifies the candidate source commit or immutable artifact.
`DRIVER_PYTHON` identifies the third environment's interpreter.
Use a new evidence directory for each invocation.

```sh
DRIVER_PYTHON tests/compare_runtime.py \
  --baseline-capture BASELINE_CAPTURE --baseline-kind controlled-artifact \
  --candidate-python CANDIDATE_PYTHON --candidate-artifact CANDIDATE_ARTIFACT \
  --candidate-source CANDIDATE_SOURCE \
  --root build/validation/independent-compatibility
```

Use `--baseline-kind controlled-artifact` for controlled package fixtures before
stable publication. The Conda CI job uses the exact candidate artifact on both
sides. The baseline comes from the runtime that passed the full suite and release
smoke tests. CI installs the candidate separately with Python 3.12.14 and requires
a different baseline Python patch. This proves that the journeys support this
runtime variation. It does not establish a published stable baseline.

After stable artifacts exist, use `--baseline-kind preserved-release` with a
complete [release bundle](../../releasing.md). The tool requires a bundle eligible
as a stable baseline from v1.0.0 onward. Obtain its identities from trusted release
attachments and pinned references. The tool does not authenticate untrusted
release metadata. The selection tool checks the complete bundle against its
repository-pinned identities before invoking this runner.

Each installation must satisfy its own package metadata requirements. Python and
dependency versions do not need to match between installations. Each runtime
must resolve its distributions, imports, and standard library inside its own
prefix. The tool rejects reused prefixes and verifies native Conda inventory,
package hashes, and candidate artifact identity. Both runtime installations must
remain unchanged through the journeys. A shared download cache does not permit
shared installed dependencies.
The comparison rejects shared directory identities, including bind mounts.
It permits hardlinks to individual files in a shared package cache.

The tool runs all 15 existing upgrade journeys against the restored baseline and
independent candidate. It writes normalized evidence to `comparison.json` under
the evidence root. The record includes runtime identities, dependency policies,
native package inventory, and journey results. Its placeholder definitions explain
normalized paths. The `journeys/` directory retains raw `environment.json`,
`transcript.jsonl`, `summary.json`, and workflow storage for inspection.
Keep raw evidence under the ignored `build/validation/` tree.
The selected record before path normalization remains in `comparison.raw.json`.
An explicit `--test` selection produces `focused-tests-passed` evidence. It does
not establish that the full upgrade matrix passed.

## Recover missing dependencies

Missing or corrupt dependencies fail restoration before the journeys run.
An unsuccessful restoration or journey is not compatibility evidence.
The tool never silently solves a different baseline runtime or skips missing cases.

If an identical dependency copy is available, append `--recovered PACKAGE` to
the comparison command. Keep the original package filename. Repeat the option
for multiple packages. Each recovered package must match the recorded SHA-256.
The original manifest remains unchanged.

If an exact dependency is permanently unavailable, document and test its replacement
under ADR 0018. Prepare a separate capture with the unchanged gwflow artifact and
the replacement dependencies. Keep the original capture and manifest.
Append both options to the comparison command:

```sh
  --replacement-capture REPLACEMENT_CAPTURE \
  --replacement-reason 'Explain the permanently unavailable dependency and its replacement'
```

The tool retains the original manifest, records the replacement and reason, and
runs the same journeys. Successful replacement evidence sets
`exact_baseline_runtime` to `false`. It does not claim to reproduce the original
dependency installation. CI also tests this route with a controlled replacement
capture and requires all 15 journeys to pass. That fixture does not claim an
actual dependency loss or establish a stable baseline.

For a selected stable release, first use `select_compatibility.py fetch` with
`--descriptor` and a new `--root` to retain its verified original bundle.
Use that bundle with `compare_runtime.py` and the recovery options above.
Retain the original descriptor and manifest with the replacement proposal and its
full passing comparison evidence. Review the proposal before changing CI to use it.
The pinned matrix intentionally continues to fail until the exact dependency is
available again or a reviewed replacement is explicitly connected to CI.
Changing an expected checksum to accept different bytes is not recovery.

## Run the provisional source comparison

The source comparison remains a provisional regression test before the first
stable publication. Its shared dependencies do not establish independent runtimes.
The [baseline descriptor](baseline.json) pins source that includes the mandatory
record-state document, persistent attempt lock, and mandatory automatic
compaction obligations from issues #254 and #259.
Its package metadata is `0.8.1`. The commit identifies the exact source.

Issue #259 requires compaction for every operation and excludes legacy-format
fallback. The previous pre-1.0 source baseline lacks the required obligations.
The new baseline keeps all compatibility journeys enabled with the new format.
Earlier validation records still describe the earlier source and remain unchanged.

Installing the same source twice tests the journey, storage handoff, and baseline
provenance. It does not prove all future upgrades. Record tested and published
artifacts through the release procedure. Pre-1.0 artifacts are controlled fixtures.
Other pre-1.0 formats, relocation, live upgrades, mixed-version workers, downgrade
support, and blanket record migration remain outside the compatibility promise.

Run from a checkout with the pinned commit available and a Python 3.12 environment
containing the project's dependencies. In this example `python` is that interpreter.
Use a fresh `build/validation/compatibility` directory. All generated packages,
environments, storage, and raw logs stay under the ignored `build/validation/` tree.

```sh
baseline_source=$(python -c 'import json; print(json.load(open("docs/validation/compatibility/baseline.json"))["source"]["commit"])')
git fetch --no-tags --depth=1 origin "$baseline_source"
mkdir -p build/validation/compatibility/source build/validation/compatibility/wheels
git archive "$baseline_source" | tar -x -C build/validation/compatibility/source
python -m pip wheel --no-deps --wheel-dir build/validation/compatibility/wheels build/validation/compatibility/source
python -m venv --system-site-packages build/validation/compatibility/baseline-env
build/validation/compatibility/baseline-env/bin/python -m pip install --no-deps --ignore-installed build/validation/compatibility/wheels/*.whl
python -m venv --system-site-packages build/validation/compatibility/candidate-env
build/validation/compatibility/candidate-env/bin/python -m pip install --no-deps --ignore-installed .
python tests/validate_compatibility.py \
  --baseline-python build/validation/compatibility/baseline-env/bin/python \
  --candidate-python build/validation/compatibility/candidate-env/bin/python \
  --baseline-source "$baseline_source" --candidate-source "$(git rev-parse HEAD)" \
  --root build/validation/compatibility/all-cases
```

These provisional environments share the base interpreter and dependency
installation. Each must contain its own installed gwflow package. The runner
records their dependency policy as `provisional-shared-runtime`. It rejects the same
environment on both sides and editable imports outside either environment. It
records each interpreter, Python/platform/dependency versions, source identifier,
installed-package content hash, and pip artifact provenance when available.
Use a clean checkout for the candidate source identifier. For a later wheel or
conda artifact, install it in the candidate environment and supply its exact source
commit or immutable artifact identifier with `--candidate-source`.

Keep the pinned source baseline unchanged when testing a later candidate.
For preserved releases, use the independent comparison procedure above.
The runner uses the selected environments for frontends and workers, removes
inherited Python import overrides, and checks the runtime installation from
retained job outputs. Fixture backend imports are restricted to the generated
workflow directory.

## Journey and evidence

The [runner](../../../tests/validate_compatibility.py) extends the installed-CLI
harness. The unchanged [workflow](../../../tests/fixtures/compatibility_workflow.py)
uses `Task`, `Workflow`, `task_template`, `shell`, named retained outputs, and a
new consumer of those outputs.

The baseline resolves an intentionally uncertain preparation admission, completes
three Tasks, and cleans one Task's work. It waits for every admitted job to finish
and stops the baseline worker before starting a candidate worker. Both installations
use the same workflow and storage paths; directory device/inode observations and
the workflow checksum verify that no relocation or rewriting was needed.

The candidate previews and runs the unchanged workflow, checks recovery is already
resolved, and proves that attempt identities, retained content, and baseline record
bytes/metadata remain unchanged. It then adds a consumer through the workflow's
declaration switch and proves that only that consumer computes. The candidate also
refuses unowned results. The ordinary suite's legacy/truncated-record tests continue
to reject unsupported ownership evidence; no migration path is introduced.

Each invocation requires a new evidence root and writes `environment.json`,
`transcript.jsonl`, and `summary.json`, retaining its workflow and storage for
inspection. Failed runs return nonzero and are not compatibility evidence.
Before stable publication, the CI `tests` job runs all compatibility journeys
before its full suite. Afterward, the Conda job runs them for each pinned stable
baseline. Both CI package suites cover ownership and unsupported-state rejection.

The incomplete-attempt journeys use the existing graph, external-input,
transfer, Repair, and recovery fixtures. They cover successful siblings alongside
failed computation, preparation interrupted before and after Input-baseline
publication, partial copying, prepared installation, installed-but-unfinished
Completion, missing retained outputs, interrupted Repair with an existing
consumer, and copying onto a separate filesystem. The candidate keeps valid
attempts and accepted input associations, executes only the necessary remainder,
and preserves checked outputs and reusable consumers. Recovery records remain
effective when scheduler history expires, without establishing computation success
or resolving a new admission.

Each journey confirms actual local jobs are terminal and stops the baseline worker
before changing installations. The local scheduler keeps history only in memory
and reuses numeric job IDs after restart. Through the existing backend fixture, the
journey therefore replays the terminal states observed before shutdown, keyed by
exact old Submission names. New submissions go to the real candidate worker.
This models a scheduler retaining its history; it does not make production code
treat unknown observations as inactivity. The recovery case separately expires
that history to verify the recorded operator assertion. Rejected admissions in
these fixtures are known not to have reached a worker.

The negative cases deliberately change commands or inputs, remove source success
evidence, simulate uncertain and active consumers, or substitute unowned results.
They prove that fresh-attempt and blockage decisions still apply. Otherwise the
workflow and all storage remain in place; no managed record conversion is used.
Separate-filesystem coverage uses the existing `/dev/shm` fixture. Missing
prerequisites or skipped cases fail the runner instead of recording acceptance.
Use repeatable `--test '*name_fragment*'` arguments for focused development;
omit them for the full matrix.

Completed-work evidence is recorded in [acceptance.json](acceptance.json), and
the extended matrix in [incomplete-acceptance.json](incomplete-acceptance.json).
Paths in each standalone record use its documented placeholders. Keep raw transcripts uncommitted
and verify path normalization against them as required by [AGENTS.md](../../../AGENTS.md).
Evidence selection summarizes the full per-file baseline snapshot by its count and
digest. Path normalization happens afterward, against that selected raw record;
it preserves versions, commands, results, hashes, and device/inode observations.
The extended record selects commands and their exit codes plus each journey's
handoff and outcome observations; full command output remains in the raw transcript.
It summarizes accepted submissions and exact-name history maps with counts and
digests before normalizing paths, as explained in that record's selection field.
