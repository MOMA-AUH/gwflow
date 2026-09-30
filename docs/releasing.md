# Conda release

The supported release environment is Python 3.12, gwf 2.1.1, and the
local backend. `pyproject.toml` and `conda-recipe/meta.yaml` carry those same
bounds. Cluster backends and other versions have not been verified.

Pull requests and pushes to `master` run the installed-package test suite and
the Conda build and installed-artifact checks in CI. The `tests` and `conda`
checks are required before merging into protected `master`. Branch protection
applies to administrators, requires linear history, and the repository permits
squash merges only.

For a release, update the version in `pyproject.toml` and
`conda-recipe/meta.yaml` together, merge the change through CI, then push a
matching `v<version>` tag on `master`. The tag workflow rejects a tag whose
version differs from `pyproject.toml` or whose commit is absent from `master`.
It builds a Conda artifact without automatic upload, installs that artifact in
a fresh environment, and runs the full local-backend test suite plus the
small-file cleanup and reuse check. Only then does the separate `publish` job
upload the same artifact to the `MOMA-AUH` Anaconda.org channel. It creates
another fresh environment from the published channel and repeats the cleanup
and reuse check. A failed upload or published-package check fails the release
workflow; do not claim publication from a successful build alone.

The `publish` job requires a repository Actions secret named
`ANACONDA_API_TOKEN` with permission to upload to the `MOMA-AUH` organization.
Store the token in GitHub Actions secrets, not in the repository. Until that
secret is configured, the reviewable build and tests can run, but the tag
workflow cannot complete publication.
