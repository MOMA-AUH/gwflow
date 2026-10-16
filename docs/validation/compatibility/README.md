# Installed compatibility baseline

[ADR 0010](../../adr/0010-preserve-public-interfaces-and-completed-work-through-1-x.md)
requires forward compatibility from v1.0.0 for public workflows and valid managed
work. The [baseline descriptor](baseline.json) pins source after mandatory command
tracking and operator recovery. Its package metadata is still `0.7.0`; the commit,
not that version string alone, identifies this bootstrap baseline.

There is no future 1.x release to test yet. Installing the same source twice
validates the journey, storage handoff, and baseline provenance. It does not prove
all future upgrades. Final release preparation and publication must identify the
actual immutable v1.0.0 artifact and checksum and exercise it as the baseline.
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
  --root build/validation/compatibility/completed
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
The CI `tests` job runs this journey before its full suite; both CI package suites
continue to cover ownership and unsupported-state rejection.

Selected evidence is recorded in [acceptance.json](acceptance.json). Paths in that
standalone record use its documented placeholders. Keep raw transcripts uncommitted
and verify path normalization against them as required by [AGENTS.md](../../../AGENTS.md).
Evidence selection summarizes the full per-file baseline snapshot by its count and
digest. Path normalization happens afterward, against that selected raw record;
it preserves versions, commands, results, hashes, and device/inode observations.
