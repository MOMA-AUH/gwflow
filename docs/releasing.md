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
The [runtime capture and restoration procedure](validation/conda-runtime.md)
checks exact dependency reconstruction before stable release preservation.
Its qualification runs in the existing Conda CI job and the tag workflow.
Pull-request validation does not publish a release.
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
The tag workflow then captures that fully tested installation. It records Python
and all Conda dependencies in a native explicit manifest with exact URLs and
SHA-256 hashes. It keeps the tested gwflow package separate from those dependencies.
It restores the capture into a new prefix with an empty package cache, installs
the source-matched fixtures without changing runtime dependencies, and runs the
existing smoke journeys. This adds one installation and smoke run, not another
full suite. Capture, reconstruction, integrity, or smoke failure stops the workflow
before publication.

After qualification, `tests/release_artifacts.py prepare` creates a checked bundle
for transfer to the `publish` job. That job verifies all bundle files and matches
the tag, source, package, manifest, and qualified dependency inventory before upload.
It uploads the same artifact to the `MOMA-AUH` Anaconda.org channel. It creates
another fresh environment from the published channel and repeats those smoke
checks. A failed upload or published-package check fails the release
workflow; do not claim publication from a successful build alone.
The smoke runner includes unresolved submission, `recover`, preview, and retry,
checking that successful siblings and original submission evidence survive.
The workflow verifies the downloaded artifact's SHA256 before upload and matches
the fresh installation's Conda metadata to that same artifact afterward.
The independently solved channel installation proves published package identity.
It does not replace the captured and reconstructed dependency environment.
Download `gwflow-publication-evidence` from the successful tag run to obtain
the three public release assets described below. `gwflow-conda-release` retains
the original flat bundle used between workflow jobs. Pre-1.0 and prerelease
versions do not establish a stable baseline.

After the tag workflow has successfully uploaded and installed the published
package, prepare the GitHub release notes for that existing tag. For a final
version, including pre-1.0 versions, use the draft that the workflow creates with
its checked attachments. For prerelease versions, create a GitHub prerelease and
attach the three files from `gwflow-publication-evidence`. Follow the previous
release notes' style: a short overview, a Highlights list, relevant compatibility
and deployment limits, and the verified setup and publication result. Link to the
tagged README and validation record, and to the MOMA-AUH Anaconda.org package.
Publish the reviewed draft only after its artifact checks pass.

The `publish` job requires a repository Actions secret named
`ANACONDA_API_TOKEN` with permission to upload to the `MOMA-AUH` organization.
Store the token in GitHub Actions secrets, not in the repository. Until that
secret is configured, the reviewable build and tests can run, but the tag
workflow cannot complete publication.
Draft attachment preparation also uses the job-scoped GitHub token with
`contents: write`. It cannot create a missing tag. It does not publish the draft.

## Stable release attachments

Final tags of the form `vMAJOR.MINOR.PATCH`, with a major version of at least one,
are eligible for stable preservation. This includes patch releases from v1.0.0
onward. Pre-1.0 tags, release candidates, development versions, and GitHub
prereleases do not establish stable baselines. A draft is not a published baseline.
This procedure does not choose a next version or extend the v1.0.0 scope.

After the published-channel identity check passes, the workflow retains three
files on the matching non-prerelease GitHub draft:

- The exact tested and published `gwflow-*.conda` package.
- `gwflow-VERSION-validation.tar.gz`, containing the validation records.
- `SHA256SUMS`, covering the package and validation archive.

The archive contains the original `runtime.explicit.txt`, `runtime.json`,
`qualification.json`, `release.json`, `publication.json`, and inner `SHA256SUMS`.
Their bytes remain unchanged. The inner checksums cover the original qualified
bundle, including the separate package. The native manifest remains usable for
runtime restoration after unpacking. The publication record proves that the
channel package matches the tested package. The archive uses fixed timestamps
and file attributes so the same records produce identical archive bytes.

The tool rechecks qualification and publication identities before any GitHub
operation. It downloads existing draft assets and accepts them only when their
bytes match. It never replaces a different asset or mutates a published release.
If attachment preparation fails, inspect the failure before publishing the draft.
Do not bypass the failure by uploading different files or removing checks.

Before publishing the draft, download its three attachments into an empty
`DOWNLOADED_ASSETS` directory. Use the intended tag and full source commit below;
`RESTORED_BUNDLE` must not exist yet:

```sh
python tests/release_artifacts.py unpack \
  --bundle DOWNLOADED_ASSETS --output RESTORED_BUNDLE \
  --tag TAG --source SOURCE_COMMIT
```

This checks both checksum lists, safe archive contents, qualification, and
publication identity before exposing the restored bundle. Review the notes, then
publish the draft. The GitHub release retains these assets after Actions artifacts
expire. CI downloads the three pinned assets and unpacks them before restoring a
stable runtime.

After successful stable publication, update the latest reference in
`docs/validation/compatibility/baseline.json` through a normal PR. Record the
published version, source commit, and every attachment URL with its expected
SHA-256 checksum. Use descriptor schema version 2 and the `assets` mapping
documented in [pinned selection](validation/compatibility/README.md#select-pinned-releases-in-ci).
Retain `stable.original` and update `stable.latest`. For the first stable
publication, replace `stable: null` with both references to that release and an
empty `intermediate` array. The selector runs identical original/latest references
once. Do not select a draft or prerelease,
invent historical artifacts, or treat the provisional source comparison as an
independent preserved environment. See [ADR 0018](adr/0018-preserve-independent-compatibility-environments.md).

Before preparing another candidate, merge this descriptor PR with passing CI.
Reviewers must compare its latest pin with the last successful stable publication.
Do not update the pin merely because a tag or draft exists. CI intentionally reads
the checkout's pins without discovering releases, so this review enforces freshness.
An old-commit rerun keeps its historical selection and cannot prove coverage of
later releases. Dependency deletion upstream can still prevent exact restoration.
Follow the [recovery procedure](validation/compatibility/README.md#recover-missing-dependencies)
without silently replacing dependencies or marking an unperformed comparison passed.

Stable compatibility covers forward upgrades of valid completed and incomplete
work with all affected jobs inactive. Keep workflow and storage paths unchanged.
It excludes live upgrades, mixed-version workers, relocation, and downgrades.
The comparisons do not broaden the [operating contract](operating.md).

Current-candidate CI exercises the same preparation and transfer checks after
real runtime qualification. Controlled release fixtures test stable attachments,
failed qualification and provenance, byte preservation, and non-stable cases.
These checks use no real release or tag publication. Keep package archives,
dependency manifests, caches, and raw logs outside Git history. This procedure
adds no dependency archives, release images, or installation method.

## Repackage an existing pre-1.0 release

For a mutable, published pre-1.0 final release with the old seven-file layout,
`tests/migrate_release_assets.py` can create the three-asset layout without
rebuilding the package or changing its tag. Published stable baselines and
immutable releases are excluded. This maintenance operation removes the five
loose metadata attachments, so their old download URLs stop working. Update
release notes to link to the archive before applying it.

Save the revised notes in `build/validation/release-assets/notes.md`. Preview the
migration with the intended repository, tag, and full source commit:

```sh
python tests/migrate_release_assets.py \
  --repo MOMA-AUH/gwflow --tag TAG --source SOURCE_COMMIT \
  --backup build/validation/release-assets/backup \
  --notes-file build/validation/release-assets/notes.md
```

The preview saves the original assets and release metadata, verifies GitHub's
asset digests, and confirms that unpacking preserves every original byte.
Repeat the command with `--apply` to upload and verify the archive, replace the
outer checksums, update the notes, and remove the loose metadata. The package
attachment is never replaced. The tool downloads and verifies the final assets.
If interrupted, rerun with the same backup directory and notes file. Keep the
backup under the ignored `build/validation/` directory until the migration has
been checked.

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
