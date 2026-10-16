# Conda release

Before v1.0.0, backward compatibility is not a release constraint; prefer the
simplest coherent design without compatibility shims or migrations, as recorded
in the [pre-1.0 development decision](adr/0001-pre-1-0-development.md).

The supported release environment is Python 3.12 and gwf 2.1.1.
`pyproject.toml` and `conda-recipe/meta.yaml` carry those same bounds.
The v0.4.0 container profile is Linux local workers and Slurm with
deployment-provided Apptainer 1.5.4. The
[validation record](validation/containers/acceptance.md) documents real execution
on both backends, the BeeGFS storage correction, and
deployment limits. Broader runtime and backend support has not been verified.
Use the [validation guide](validation/README.md) for current setup and checks,
and its evidence index to find the relevant historical records.

Pull requests and pushes to `master` run the installed-package test suite and
the Conda build and installed-artifact checks in CI. The `tests` and `conda`
checks are required before merging into protected `master`. The `tests` job also
executes real Apptainer containers and the consolidated local acceptance cases;
the Conda job reports container-runtime skips explicitly. Live Slurm evidence is
recorded separately in the validation record. Branch protection
applies to administrators with no bypass actors, requires a pull request and
linear history, and the repository permits squash merges only. Reviewer approval
is optional; the branch must be up to date with `master` before merging.

For a release, ensure the version in `pyproject.toml` and
`conda-recipe/meta.yaml` matches the intended release, updating them together if
needed. Merge the release preparation PR through CI, then push a
matching `v<version>` tag on `master`. The tag workflow rejects a tag whose
version differs from `pyproject.toml` or whose commit is absent from `master`.
It builds a Conda artifact without automatic upload, installs that artifact in
a fresh environment, and runs the full local-backend test suite plus the
small-file cleanup/reuse and troubleshooting/cancellation/recovery smoke checks.
Only then does the separate `publish` job
upload the same artifact to the `MOMA-AUH` Anaconda.org channel. It creates
another fresh environment from the published channel and repeats those smoke
checks. A failed upload or published-package check fails the release
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

## Planned v1.0.0 acceptance scope

The release plan below was agreed on 2026-10-16. It records intended work and
release requirements, not completed implementation or validation.

v1.0.0 targets the maintainer's production pipelines on the current
Linux/Slurm/Apptainer infrastructure, with local workers for development and
testing. Independent adoption across a broader range of deployments is not a
release requirement.

### Agreed work and order

1. Make command tracking mandatory in managed workflows and reject
   `use_spec_hashes=false` with guidance to remove the setting
   ([ADR 0011](adr/0011-track-command-changes-by-default.md)). Keep explicit
   `gwflow run TASK --force` for untracked changes, add no Reuse key, and preserve
   cleanup's existing meaning
   ([ADR 0012](adr/0012-use-explicit-force-for-untracked-changes.md)).
2. Add `gwflow recover TASK` for unresolved submissions, following the operator
   assertion, whole-Task inactivity, current-attempt scope, evidence preservation,
   and partial-work recovery contract in
   [ADR 0013](adr/0013-provide-explicit-recovery-for-unresolved-submissions.md).
   It takes no additional arguments or flags, asks no interactive confirmation,
   and submits no jobs.
3. Review and settle final CLI terminal presentation against those behaviors,
   as described below.
4. Complete compatibility validation and documentation for the 1.x baseline
   ([ADR 0010](adr/0010-preserve-public-interfaces-and-completed-work-through-1-x.md)).
   Preserve public interfaces, completed results after work cleanup, valid partial
   computation, and interrupted-installation recovery across forward 1.x
   upgrades made with jobs confirmed inactive. Establish automated regression
   coverage and baseline evidence for future upgrades; do not promise blanket
   support for pre-1.0 records. Reconcile the glossary's Completion semantics
   with the managed implementation and document upgrade and recovery procedures.
5. Complete the automated, installed-package, and publication checks for release.

Broader platform support, structured JSON output, storage relocation, and
unrelated refactoring are outside this release scope. Internal fixes necessary
to meet the agreed guarantees remain included.

### Release acceptance

Automated tests and the existing examples are the release acceptance basis,
alongside the installed-artifact and publication checks described above.
Representative pipeline validation on the actual cluster is follow-up work,
not a gate for v1.0.0. Release evidence must distinguish the automated checks
performed for the candidate from historical live-cluster evidence; passing the
former does not establish that the candidate passed a representative production
pipeline on Slurm.

Final CLI terminal output must be settled before v1.0.0. The maintainer has
additional presentation wishes that are not yet specified, so this remains an
open design milestone within the agreed release plan. The review covers layout,
wording, colors, grouping, progress, verbosity
of reports, and error/recovery guidance. Command names, arguments, flags, and
behavior remain governed by the settled command decisions; this review does not
reopen them. The detailed presentation changes and review criteria remain to be
agreed.
