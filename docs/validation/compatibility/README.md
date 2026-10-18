# Installed compatibility baseline

[ADR 0010](../../adr/0010-preserve-public-interfaces-and-completed-work-through-1-x.md)
requires forward compatibility from the future v1.0.0 baseline for public
workflows and valid managed work. The current release target is v0.8.0, under
the pre-1.0 policy; these journeys remain regression checks and preparation for
the stable baseline. The [baseline descriptor](baseline.json) pins source after
mandatory command tracking and operator recovery. Its package metadata is still `0.7.0`; the commit,
not that version string alone, identifies this bootstrap baseline.

There is no future 1.x release to test yet. Installing the same source twice
validates the journey, storage handoff, and baseline provenance. It does not prove
all future upgrades. v0.8.0 preparation and publication must record the exact
tested and published artifacts, without calling them the stable compatibility
baseline. When v1.0.0 is prepared and published, identify its actual immutable
artifact and checksum and exercise it as the baseline for later 1.x candidates.
Other pre-1.0 formats, relocation, live upgrades, mixed-version workers, downgrade
support, and blanket record migration remain outside the compatibility promise.

## Build independent installations

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

The environments can share the base interpreter and dependency installation, but
each must contain its own installed gwflow package. The runner rejects the same
environment on both sides and editable imports outside either environment. It
records each interpreter, Python/platform/dependency versions, source identifier,
installed-package content hash, and pip artifact provenance when available.
Use a clean checkout for the candidate source identifier. For a later wheel or
conda artifact, install it in the candidate environment and supply its exact source
commit or immutable artifact identifier with `--candidate-source`.

Keep the pinned baseline unchanged when testing a later candidate. An actual
released baseline can be installed from its preserved artifact instead of rebuilding
the bootstrap source; record its artifact identifier with `--baseline-source`.
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
The CI `tests` job runs all compatibility journeys before its full suite; both CI package suites
continue to cover ownership and unsupported-state rejection.

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
