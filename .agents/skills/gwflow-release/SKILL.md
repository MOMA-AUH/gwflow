---
name: gwflow-release
description: 'Make a gwflow release when the user says "Make release vX.Y.Z", "Make vX.Y.Z release", or requests a concrete version. Propose release notes for agreement, then deliver through protected PRs, CI, Conda publication, asset verification, and GitHub publication. Release questions and workflow edits do not authorize a release.'
---

# gwflow release

Read [releasing](../../../docs/releasing.md),
[contributing](../../../.github/CONTRIBUTING.md), and the current
[release workflow](../../../.github/workflows/release.yml). Follow those sources
for package qualification, retries, note formatting, and stable baseline pins.
Read the [maintainer-led HPC context](../../../docs/validation/README.md#maintainer-led-hpc-testing)
before planning validation. Routine cluster testing adds no release approval gate.
Use `gh` with `--repo MOMA-AUH/gwflow` for GitHub operations.

## Propose notes first

A concrete release request starts this process. A question, hypothetical
example, or request to edit the process is documentation work only. If the
version is missing or literally `vX.Y.Z`, ask for the intended version.

1. Inspect the working tree, fetch remote master and tags, and inspect published
   releases. Preserve unrelated edits; use an isolated checkout when needed.
   Base the scope on committed, merged work. Identify the candidate master
   commit and previous published release used for comparison.
2. Validate canonical `vMAJOR.MINOR.PATCH` with no leading zeroes and ensure the
   version advances the previous release. Publish a normal release by default,
   including pre-1.0 versions. Support a suffix only for an explicitly requested
   prerelease, checking it against existing package/tag rules. If the version is
   already published, report its URL rather than replacing it. Inspect existing
   tags, drafts, or preparation PRs as possible resume points.
3. Propose notes covering shipped features, fixes, requirements, compatibility,
   and deployment limits, following previous notes and the release guide.
   Separate observed validation from planned checks. Save the notes and a state
   record under ignored `build/validation/releases/VERSION/`. Record repository,
   version, candidate commit, comparison base, and approval status **pending**.
4. Show the proposed notes and request explicit agreement. End the turn here,
   with version files, PRs, tags, and remote releases unchanged. Incorporate edits
   and show revised notes for agreement. An edit request or elapsed time is not
   approval.

## Deliver after agreement

Explicit agreement to the proposed notes authorizes preparation PRs, their merge
after passing checks, the tag push, Conda publication, GitHub publication, and
the final notes/baseline PR. Record the approval message, approved notes, and
source revision. A saved draft alone establishes no approval. Resume only from
actual agreement and verified remote state.

1. Recheck remote state against the approved source. If release contents or
   substantive notes change, present revised notes for agreement. Routine version
   metadata and verified publication results are part of approved delivery.
2. Prepare a focused release branch/PR when needed. Update `pyproject.toml` and
   `conda-recipe/meta.yaml` together, the README example checkout tag, and any
   packaged Task fixture dependency bounds that exclude the candidate. Preserve
   the fixtures' independent package versions. Stage proposed notes in
   `docs/releases/<tag>.md`, identifying unfinished verification accurately.
   Reuse a matching preparation PR. Skip preparation only when the approved
   merged source already contains the necessary metadata and documentation.
3. Run appropriate local checks from the validation guide. Inspect effective
   branch protection and wait for every required check on the current, up-to-date
   PR head. Squash-merge using that exact head SHA and record the resulting master
   commit. Use the approved existing master commit if preparation was unnecessary.
   Apply this protected PR process to subsequent archive/baseline changes too.
4. Create an annotated tag at the recorded release commit and push only that tag
   using authenticated user Git so Actions runs. Reuse an existing tag only after
   verifying its peeled commit matches the approved release source. Preserve
   existing tags. Moving or force-pushing a release tag is outside this workflow.
5. Monitor `release-qualification.yml` for that exact tag and source through
   successful completion. It runs the reusable full qualification pipeline after
   checking master membership and package versions. A green PR run alone is
   insufficient. Record the trusted run and immutable artifact ID with
   `tests/select_ci_release.py`. Follow the release guide for manual qualification
   without publication, qualification retries, or expired artifacts.
6. Monitor `release.yml` after successful tag qualification through verification
   and publication. It publishes the exact qualified package to Anaconda.org,
   verifies a fresh channel installation, and stages final-version draft assets.
   Keep its selected qualification bundle on retries. Follow the interrupted
   publication guide rather than rebuilding or substituting artifacts.
   Retry a transient failure at most once for the same cause, then report a
   repeated blocker. Different tagged code requires a new version agreed with
   the user. Ordinary merges do not start full master CI.
7. Verify the draft's tag resolves to the recorded source. Download all three
   draft assets to a fresh directory and run the existing verifier:

   ```sh
   python tests/release_artifacts.py unpack \
     --bundle ASSET_DIRECTORY --output NEW_VERIFIED_DIRECTORY \
     --tag TAG --source FULL_SOURCE_COMMIT
   ```

   Replace uppercase arguments with actual paths, tag, and full SHA. Confirm
   package, checksums, qualification, and publication provenance agree with the
   selected CI bundle. For an explicit prerelease, obtain and verify the three
   assets from `gwflow-publication-evidence`, then create a prerelease draft for
   the existing tag and attach those verified bytes as the guide specifies.
   Preserve conflicting assets and report the discrepancy.
8. Apply agreed notes with verified CI/publication results and source/run links.
   Keep the substantive approved scope. Publish the checked draft without another
   confirmation. Explicitly set normal release status unless a prerelease was
   requested. Follow existing repository practice for latest promotion and verify
   the resulting status, tag, notes, and three assets from GitHub.
9. Synchronize the final GitHub description into `docs/releases/<tag>.md` through
   a focused PR and verify exact text equality. For a stable release eligible
   under the guide, include verified baseline descriptor updates in this PR,
   preserving the original baseline. Merge after required CI passes. Keep raw
   logs and state under ignored `build/validation/`; normalize any committed
   validation records according to AGENTS.md. Synchronize local master when
   doing so preserves user work.

Record PRs, merge/source SHAs, CI/artifact IDs, tag, publication run, asset hashes,
and final release URL as delivery proceeds. Completion requires verified Conda
and GitHub publication plus merged final notes and applicable baseline pins.
Report the URL, source, checks, and any material verification limit or remaining
blocker. Preserve published releases. Missing approval or conflicting provenance
requires reporting the specific missing evidence rather than publishing.
