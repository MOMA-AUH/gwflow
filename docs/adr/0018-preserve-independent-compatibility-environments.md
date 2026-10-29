# Preserve independent environments for compatibility tests

Preserve each stable compatibility baseline's gwflow package with fixed Python and
dependency versions. Run the baseline and candidate in independent installations,
with the candidate using its own supported environment. The baseline must not
share the candidate's dependency installation. This makes the older environment
reproducible and tests dependency changes alongside the gwflow upgrade.

Record exact dependency builds, download URLs, checksums, and the target platform
in a manifest without archiving the dependency package files. Reconstruct the
baseline dependencies from their upstream package repositories. This limits
storage requirements while accepting that recreation
depends on those builds remaining available upstream.

Generate the manifest automatically from the runtime environment that passed
the full release tests. Include Python and all runtime dependencies, including
indirect dependencies and native libraries.

Use Conda's native explicit package-list format for this manifest. Record the
target platform and exact package URLs with SHA-256 checksums. Require a URL
and checksum for every runtime dependency before accepting the export.
Conda can then install those exact packages without solving for new versions.
This uses existing Conda export and installation tools instead of a custom
JSON manifest format.
Keep the gwflow package as a separate release attachment and use it with the
dependency manifest during restoration.

Before publishing a stable release, recreate a clean environment from its
manifest and the exact gwflow Conda package. Install the recorded builds and
verify their checksums, then run the existing release smoke tests. Add this
check to release verification using the restoration procedure that future
compatibility tests will use. This adds one installation and smoke-test run
and checks that the saved manifest can recreate the tested package environment
at publication time.

For every stable release from v1.0.0 onward, including patch releases, attach
the exact tested and published Conda package, its checksum, and its dependency
manifest to the GitHub release. Keep these files outside Git history and verify
the package checksum before installing a baseline. This preserves the gwflow
package independently of the Conda channel and temporary CI artifacts.

Once stable compatibility applies, test each candidate against the original
stable baseline and the most recent stable release. Add an intermediate baseline
only when it covers a compatibility case those two cannot exercise. This keeps
the routine test matrix manageable while covering older installations and recent
upgrades, including record states and features introduced after the original
baseline.

Pin the original and latest stable baseline references in the repository's
`docs/validation/compatibility/baseline.json`. Record release versions, download
URLs, and expected checksums for both the gwflow packages and their manifests.
Update the latest reference after each successful stable publication.

CI uses the references in its checkout. Rerunning an old commit therefore
repeats its original baseline selection. New release candidates must use an
updated descriptor to cover the latest stable release. This requires descriptor
maintenance after releases, but makes baseline selection explicit and
reproducible. The packages and manifests themselves remain release attachments.

Release container images are outside this preservation policy. We accept the
dependency-availability risk to avoid the storage and CI maintenance required
for an image of every release. Conda remains the supported installation method.

If an exact dependency becomes permanently unavailable, fail compatibility CI
with a clear error until an identical copy is recovered or a replacement
environment is documented and validated. Verify recovered copies against the
recorded checksum. Keep the preserved gwflow package unchanged when replacing
dependencies, retain the original manifest, and record the replacement manifest
and the reason for the change. A replacement environment no longer reproduces
the original dependency installation exactly. CI must not silently substitute
dependencies or report an unperformed compatibility test as passing.

Before the first stable publication, keep the existing source-based comparison
as a provisional regression check. Its shared dependencies do not establish an
independent preserved baseline. Prepare and test the manifest restoration
process before that publication. Once the stable artifacts exist, compare later
candidates against the preserved stable releases under this policy. This does
not require retroactive preservation of pre-1.0 dependency environments or
promise compatibility with pre-1.0 releases.

This work prepares compatibility testing without choosing a release target.
The [pre-1.0 policy](0001-pre-1-0-development.md) and
[future stable compatibility contract](0010-preserve-public-interfaces-and-completed-work-through-1-x.md)
remain in effect.
