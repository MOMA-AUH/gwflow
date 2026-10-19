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
Operators should use the [operating and upgrade guide](operating.md) for force,
cleanup, unresolved submissions, and upgrades with all affected jobs inactive.

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
The smoke runner includes unresolved submission, `recover`, preview, and retry,
checking that successful siblings and original submission evidence survive.
The workflow verifies the downloaded artifact's SHA256 before upload and matches
the fresh installation's Conda metadata to that same artifact afterward.
Download `gwflow-conda-release` and `gwflow-publication-evidence` from the successful
tag run to retain the package, `SHA256SUMS`, and `publication.json`. Attach the
provenance files to the GitHub release so its source commit, package URL, checksum,
runtime versions, and workflow result remain discoverable after Actions artifacts
expire. These identities describe the pre-1.0 release, not a stable baseline.

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

## v0.8.0 acceptance scope

The release plan below was agreed on 2026-10-16 and retargeted from v1.0.0 to
v0.8.0 on 2026-10-18. It records the release requirements. Feature delivery is
recorded in [ADR 0014](adr/0014-make-terminal-status-interactive-by-default.md),
and candidate checks and limits in the [v0.8.0 validation record](validation/v0.8.0/acceptance.md).
The [release notes](releases/v0.8.0.md) are prepared for publication after the tag
workflow succeeds. v0.8.0 remains a pre-1.0 release;
the future v1.0.0/1.x compatibility policy does not become a v0.8.0 guarantee.

v0.8.0 targets the maintainer's production pipelines on the current
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
4. Retain the installed compatibility journeys as regression checks and
   preparation for the future stable baseline
   ([ADR 0010](adr/0010-preserve-public-interfaces-and-completed-work-through-1-x.md)).
   Check public interfaces, completed results after work cleanup, valid partial
   computation, and interrupted-installation recovery with jobs confirmed
   inactive. Record the exact tested baseline and candidate without promising
   compatibility for untested pre-1.0 releases. v0.8.0 publication does not
   establish the future v1.0.0 baseline. Reconcile the glossary's Completion
   semantics with the managed implementation and document upgrade and recovery
   procedures.
5. Complete the automated, installed-package, and publication checks for release.

Broader platform support, structured JSON output, storage relocation, and
unrelated refactoring are outside this release scope. Internal fixes necessary
to meet the agreed guarantees remain included.

### Release acceptance

Automated tests and the existing examples are the release acceptance basis,
alongside the installed-artifact and publication checks described above.
Representative pipeline validation on the actual cluster is follow-up work,
not a gate for v0.8.0. Release evidence must distinguish the automated checks
performed for the candidate from historical live-cluster evidence; passing the
former does not establish that the candidate passed a representative production
pipeline on Slurm.

CI runners must have no access to the production cluster. The maintainer ruled
out a cluster-connected runner on 2026-10-18 because this is a public repository.
Automated backend coverage uses local workers and the Slurm adapter with
controlled replacement Slurm commands. Interactive presentation validation uses
a 100-Task/1,000-job captured workload. Functional and resource-bound checks run
in CI; reproducible performance measurements are automated and recorded
separately, without tight wall-clock thresholds gating ordinary CI or requiring
the maintainer to collect benchmark results manually. These checks do not replace
or claim representative production-cluster validation.

Final CLI terminal output must be settled before v0.8.0. The agreed decisions
are recorded in ADR 0014; the maintainer approved the ten implementation slices
and validation requirements on 2026-10-18 in
[issue #213](https://github.com/MOMA-AUH/gwflow/issues/213)
within the agreed release plan. The review covers layout,
wording, colors, grouping, progress, verbosity
of reports, and error/recovery guidance. On 2026-10-17, the maintainer expanded
this scope to include interactive status, now targeted for v0.8.0, defaulting to
that view in an interactive terminal, with `status --static` for one formatted report,
`status --plain` for one undecorated report, and automatic plain static output
when redirected or piped
([ADR 0014](adr/0014-make-terminal-status-interactive-by-default.md)). This
intentionally revises the earlier presentation-only restriction. Task-selection
semantics and ordinary execution safeguards remain in force. The v0.8.0 TUI is for
inspection, with manual status refresh, in-app logs and history, and optional
live log following; run and cancel remain ordinary commands, exposed through
copyable command hints. All ten approved implementation tickets are required
blockers of release preparation #217. All ten are now merged with required CI;
candidate validation and publication remain separate from that feature delivery.
