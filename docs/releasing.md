# Conda release

Before v1.0.0, backward compatibility is not a release constraint; prefer the
simplest coherent design without compatibility shims or migrations, as recorded
in the [pre-1.0 development decision](adr/0001-pre-1-0-development.md).

The supported release environment is Python 3.12 and gwf 2.1.1.
`pyproject.toml` and `conda-recipe/meta.yaml` carry those same bounds.
The v0.4.0 container profile is Linux local workers and Slurm with
deployment-provided Apptainer 1.5.4. The [validation record](validation-v0.4.0.md)
documents real execution on both backends, the BeeGFS storage correction, and
deployment limits. Broader runtime and backend support has not been verified.

Pull requests and pushes to `master` run the installed-package test suite and
the Conda build and installed-artifact checks in CI. The `tests` and `conda`
checks are required before merging into protected `master`. The `tests` job also
executes real Apptainer containers and the consolidated local acceptance cases;
the Conda job reports container-runtime skips explicitly. Live Slurm evidence is
recorded separately in the validation record. Branch protection
applies to administrators, requires linear history, and the repository permits
squash merges only.

For a release, ensure the version in `pyproject.toml` and
`conda-recipe/meta.yaml` matches the intended release, updating them together if
needed. Merge the release preparation PR through CI, then push a
matching `v<version>` tag on `master`. The tag workflow rejects a tag whose
version differs from `pyproject.toml` or whose commit is absent from `master`.
It builds a Conda artifact without automatic upload, installs that artifact in
a fresh environment, and runs the full local-backend test suite plus the
small-file cleanup and reuse check. Only then does the separate `publish` job
upload the same artifact to the `MOMA-AUH` Anaconda.org channel. It creates
another fresh environment from the published channel and repeats the cleanup
and reuse check. A failed upload or published-package check fails the release
workflow; do not claim publication from a successful build alone.

After the tag workflow has successfully uploaded and installed the published
package, create the GitHub release for that existing tag. Follow the previous
release notes' style: a short overview, a Highlights list, relevant compatibility
and deployment limits, and the verified setup and publication result. Link to the
tagged README and validation record, and to the MOMA-AUH Anaconda.org package.

The `publish` job requires a repository Actions secret named
`ANACONDA_API_TOKEN` with permission to upload to the `MOMA-AUH` organization.
Store the token in GitHub Actions secrets, not in the repository. Until that
secret is configured, the reviewable build and tests can run, but the tag
workflow cannot complete publication.
