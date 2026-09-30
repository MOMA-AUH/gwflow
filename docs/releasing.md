# Conda release

The supported environment uses Python 3.12 and gwf 2.1.1.
`pyproject.toml` and `conda-recipe/meta.yaml` carry those bounds. Local-backend
CI runs on every PR. Real-Slurm acceptance is limited to the tested
`www.genome.au.dk` site and configuration; it does not imply support for every
Slurm installation, Python version, or gwf version.

Pull requests and pushes to `master` run the installed-package test suite and
the Conda build and installed-artifact checks in CI. The `tests` and `conda`
checks are required before merging into protected `master`. Branch protection
applies to administrators, requires linear history, and the repository permits
squash merges only.

Configure the [self-hosted Slurm runner](slurm-runner.md) before attempting a
release. Development acceptance can be triggered manually without Slurm on
every PR.

For a release, update the version in `pyproject.toml` and
`conda-recipe/meta.yaml` together, merge the change through CI, then push a
matching `v<version>` tag on `master`. The tag workflow rejects a tag whose
version differs from `pyproject.toml` or whose commit is absent from `master`.
It builds a Conda artifact without automatic upload, installs that artifact in
a fresh environment, and runs the full local-backend test suite plus the
small-file cleanup and reuse check. The same candidate artifact is downloaded
by the self-hosted runner, installed on shared HPC storage, and tested through
real Slurm submissions. Only after both gates pass does the separate `publish`
job upload that artifact to the `MOMA-AUH` Anaconda.org channel. It creates
another fresh environment from the published channel and repeats the cleanup
and reuse check. A failed upload or published-package check fails the release
workflow; do not claim publication from a successful build alone.

The `publish` job requires a repository Actions secret named
`ANACONDA_API_TOKEN` with permission to upload to the `MOMA-AUH` organization.
Store the token in GitHub Actions secrets, not in the repository. Until that
secret is configured, the reviewable build and tests can run, but the tag
workflow cannot complete publication.
