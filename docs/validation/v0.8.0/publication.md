# v0.8.0 publication

This record covers [publication issue #218](https://github.com/MOMA-AUH/gwflow/issues/218).
The tag workflow published and verified the release on 2026-10-21.
The release remains pre-1.0 and does not establish the future
v1.0.0 compatibility baseline.

## Source and prerequisites

The release tag `v0.8.0` identifies source commit
`e4d8bd2b86d0c0454c6192669029e8b5f71f4128`.
[PR #250](https://github.com/MOMA-AUH/gwflow/pull/250) delivered this commit through
protected `master`. Its required `tests` and `conda` checks passed in
[run 39632416964](https://github.com/MOMA-AUH/gwflow/actions/runs/39632416964).
The merged commit and the tested PR head have the same Git tree,
`1e407127e01b6634c2e737684a88a9598f74fc96`.

Both checks also passed on the merged commit in
[master CI run 39637509223](https://github.com/MOMA-AUH/gwflow/actions/runs/39637509223).
The source job passed 607 tests without skips, 15 compatibility cases, all three
smoke journeys, and 14 real Apptainer/local acceptance cases.
The Conda job ran 607 tests: 563 passed and 44 container-dependent cases skipped.
All three Conda smoke journeys passed.

[Candidate preparation #217](https://github.com/MOMA-AUH/gwflow/issues/217) is closed.
All ten presentation tickets, #226–#235, are closed. The
[candidate acceptance record](acceptance.md) describes earlier compatibility
journeys and measurements. Those historical measurements retain their original
source and artifact identities.

## Publication checks

[Tag workflow 39637951722](https://github.com/MOMA-AUH/gwflow/actions/runs/39637951722)
uses the release commit's
[publication workflow](https://github.com/MOMA-AUH/gwflow/blob/e4d8bd2b86d0c0454c6192669029e8b5f71f4128/.github/workflows/release.yml).
It checks tag/version agreement and membership in `master` before building.
The verification job installs the built Conda package into a fresh environment.
It runs the full installed suite and all three release smoke journeys.

The publication job checks the downloaded artifact's SHA256 before upload.
It installs `gwflow=0.8.0` from `MOMA-AUH` into another fresh environment and
checks the channel. It runs the packaged cleanup/Reuse, troubleshooting, and
operator-recovery smoke journeys. The final provenance check matches the
installed filename, version, channel URL, and SHA256 to the tested artifact.

Both release jobs succeeded. The artifact suite ran 607 tests in 1552.815 seconds:
563 passed and 44 container-dependent cases skipped. All three artifact smoke
journeys passed in 11.048 seconds. After upload, all three published-package smoke
journeys passed in 11.069 seconds, and the provenance check passed.

The exact published package is
[`gwflow-0.8.0-py_0.conda`](https://conda.anaconda.org/MOMA-AUH/noarch/gwflow-0.8.0-py_0.conda),
83,896 bytes, with SHA256
`36252c9e3af0cf63ce510728fc61d88dfcb0146abc16906dc2b7da65ad3b1da2`.
[publication.json](publication.json) records the source, artifact, workflow, and
runtime versions. The channel installation used Linux x86_64, Python 3.12.15,
gwf 2.1.1, Rich 15.0.0, and Textual 8.2.8.
[SHA256SUMS](SHA256SUMS) retains the workflow-generated checksum.

## Commands and retained evidence

Run these commands from the release checkout. `<RUNNER_TEMP>` denotes GitHub's
temporary runner directory, outside the checkout. `<RELEASE_PYTHON>` and
`<PUBLISHED_PYTHON>` denote the Python executables in the fresh artifact and
channel environments respectively. `<ARTIFACT>` denotes the exact Conda filename
recorded in `publication.json`. All other paths are relative to the checkout.
The commands below expand the workflow's version and source variables and omit
environment bootstrap commands, which remain in the linked workflow.

```sh
conda build conda-recipe --no-anaconda-upload -c gwforg -c conda-forge --output-folder <RUNNER_TEMP>/conda-dist
conda create -y -p <RUNNER_TEMP>/gwflow-release-test python=3.12 'gwflow=0.8.0' -c file://<RUNNER_TEMP>/conda-dist -c gwforg -c conda-forge
<RELEASE_PYTHON> -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
<RELEASE_PYTHON> -m unittest discover -s tests -v
<RELEASE_PYTHON> tests/release_smoke.py
```

The workflow writes `SHA256SUMS` in `<RUNNER_TEMP>/conda-dist/noarch` with
`sha256sum gwflow-*.conda > SHA256SUMS`. It transfers the package and checksum
through the `gwflow-conda-release` Actions artifact. The publication job checks
`sha256sum --check SHA256SUMS` from its downloaded `conda-dist` directory.

```sh
conda run -n base anaconda upload --user MOMA-AUH --register --label main --no-progress conda-dist/<ARTIFACT>
conda create -y -p <RUNNER_TEMP>/gwflow-published-test python=3.12 'gwflow=0.8.0' --override-channels -c MOMA-AUH -c gwforg -c conda-forge
<PUBLISHED_PYTHON> -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
<PUBLISHED_PYTHON> tests/release_smoke.py
<PUBLISHED_PYTHON> tests/release_provenance.py --artifacts conda-dist --source e4d8bd2b86d0c0454c6192669029e8b5f71f4128 --tag v0.8.0 --output conda-dist/publication.json
```

Raw downloaded logs and artifacts stay under ignored
`build/validation/v0.8.0/publication/`. The committed provenance and checksum are
byte-identical copies of the workflow outputs. They contain no machine-specific
paths and required no normalization. The downloaded release package also passed
an independent SHA256 comparison against both workflow evidence artifacts.
The [GitHub release](https://github.com/MOMA-AUH/gwflow/releases/tag/v0.8.0)
retains `publication.json` and `SHA256SUMS` as permanent release assets.

## Scope and limitations

The release checks use hosted Linux workers and local execution. Automated Slurm
tests use controlled replacement commands without production-cluster access.
The separate source CI job supplies real Apptainer fixtures. The Conda and tag
suites report container cases as skipped when those fixtures are absent.
All 44 skips in each Conda suite require the absent container fixtures.
The source job supplied those fixtures and reported no skips.

Historical live Slurm results remain in the
[container acceptance record](../containers/acceptance.md) and
[registry acceptance record](../registry-images/acceptance.md).
Representative production-pipeline validation remains follow-up work, outside
the v0.8.0 release gate. This publication does not establish broader platform
support, storage relocation, live upgrades, or mixed-version worker support.
