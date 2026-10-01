# v0.4 container validation

This record follows the incremental implementation of
[the v0.4 specification, #92](https://github.com/MOMA-AUH/gwflow/issues/92).
The first slices, [#93](https://github.com/MOMA-AUH/gwflow/issues/93) and
[#94](https://github.com/MOMA-AUH/gwflow/issues/94), validate image execution,
frontend image identity, external-input staging, failure, and cleanup/Reuse.
[#95](https://github.com/MOMA-AUH/gwflow/issues/95) extends those checks to mixed
graphs and retained-output consumers, and
[#96](https://github.com/MOMA-AUH/gwflow/issues/96) covers custom staged names.
[#97](https://github.com/MOMA-AUH/gwflow/issues/97) covers environment and scratch.
[#98](https://github.com/MOMA-AUH/gwflow/issues/98) validates failure, retry and repair.
[#99](https://github.com/MOMA-AUH/gwflow/issues/99) adds the packaged container demonstration.
[#100](https://github.com/MOMA-AUH/gwflow/issues/100) consolidates the checks and
records the live backend validation below. This is implementation validation, without
release publication or deployment provisioning.

## Repeatable local checks

Use Python 3.12, gwf 2.1.1, and deployment-provided Apptainer 1.5.4 on Linux.
Install the artifacts and prepare a test image from the repository root:

```sh
python -m pip install .
python -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
mkdir -p build/validation
apptainer build build/validation/basic.sif tests/fixtures/container.def
apptainer build build/validation/summary.sif examples/packaged/images/summary.def
apptainer build build/validation/report.sif examples/packaged/images/report.def
export GWFLOW_TEST_SIF="$PWD/build/validation/basic.sif"
export GWFLOW_TEST_SUMMARY_SIF="$PWD/build/validation/summary.sif"
export GWFLOW_TEST_REPORT_SIF="$PWD/build/validation/report.sif"
python -m unittest discover -s tests -p test_containers.py -v
python -m unittest discover -s tests -p test_staging.py -v
python -m unittest discover -s tests -p test_container_graphs.py -v
python -m unittest discover -s tests -p test_container_environment.py -v
python -m unittest discover -s tests -p test_container_recovery.py -v
python -m unittest discover -s tests -p test_example.py -v
python -m unittest discover -s tests -v
python tests/release_smoke.py
```

Image preparation is fixture/deployment setup, not a gwflow acquisition feature.
The fixture uses the official Python image and adds `gwflow-image-tool`, an
image-only command that checks gwflow is absent and prints the image's configured
environment value. The build requires network access to the base image registry.
The runtime tests use real Apptainer and local workers, including paths with
spaces, and explicitly skip if `GWFLOW_TEST_SIF` is unset. A set but unusable
fixture fails. CI's installed-package job prepares this image and runs those
checks; its built Conda artifact job continues the complete host suite and
packaged example smoke. Skipped container tests do not count as runtime evidence.

## Local runtime observations

On 2026-10-01, the focused container checks ran on Linux with Python 3.12.14,
gwf 2.1.1 and the development implementation. The isolated validation runtime
was the upstream `apptainer-1.5.4-1.x86_64.rpm`, unpacked below the ignored
`build/validation/runtime` directory. Its `usr/etc` and `usr/var` links point to
the extracted configuration and state directories; system Apptainer was not
modified. `apptainer --version` reported `1.5.4-1`.

The #93–98 installed-package validation used a built wheel with source metadata `0.3.1`
and the new image-aware record requirements, plus summary/report packages
`0.2.0`. The #99 package metadata advances to gwflow `0.4.0` and example packages
`0.3.0`, which require gwflow `>=0.4,<0.5`. These are local/CI artifacts, without
a release tag or publication. All test state is freshly created by the
implementation under test.

The fixture's recorded OCI base was `docker.io/library/python:3.12-slim-bookworm`,
digest `sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e`.
The extracted default configuration enabled home, temporary, proc, sys, dev,
and devpts mounts, disabled `mount hostfs`, and retained `/etc/localtime` and
`/etc/hosts` binds. No gwflow-specific site bind or container environment was
needed. The tests deliberately set host `PYTHONPATH` pollution.

Focused evidence in [test_containers.py](../tests/test_containers.py) covers:

- Image-only command execution without gwflow in the image; private writable
  scratch; checked retained output; zero-submission Reuse before and after work
  cleanup.
- Independent size, modification-time, and resolved-path changes, including
  equal-metadata alias retargeting, with command tracking disabled. Preview
  snapshots preserve evidence, work, and results; a later run starts fresh.
- Equivalent aliases with command tracking enabled; temporarily unavailable
  images blocking every frontend decision without deleting results; matching
  restoration resuming Reuse.
- A deterministic preparation gate followed by alias retargeting and image
  metadata change. The already scheduled command still executes the original
  resolved image and finishes; jobs do not reobserve image identity.
- Nonzero exits after writing outputs, missing image software, missing/invalid
  outputs, and unusable images refusing Completion with normal target/image logs.
- An empty computation-job PATH makes missing Apptainer fail without fallback;
  the same deployment condition permits an ordinary host Bash command.
- Rejection of unsupported earlier attempt evidence, preserving its files.

The preparation gate and execution-environment fault controls are test-only
backend fixtures. They control timing/environment around real scheduled host
lifecycle processes; they do not simulate Apptainer's command execution.

The #93 final-commit [CI run](https://github.com/MOMA-AUH/gwflow/actions/runs/36877118052)
passed all 215 tests against the pip-installed package with Apptainer 1.5.4 and no
skips. The Conda artifact suite passed with eight explicit container-test skips,
and its packaged smoke passed. The separate local installed-package suite also
passed all 215 tests.

The #94 checks in [test_staging.py](../tests/test_staging.py) use the same real
Apptainer runtime and fixture. All five focused tests passed on 2026-10-01:

- A source parent containing execution work remains readable, rejects writes
  through the staged source link, and permits writes in work and private TMPDIR.
  Copying the input into work produces an editable copy and retained output.
- An input alias through a symlinked parent reaches a destination outside the
  alias directory. Source-parent paths containing commas, quotes, spaces, colons,
  and dollar signs, and staged basenames containing shell punctuation, work.
- Only declared inputs receive staged symlinks. An undeclared neighboring file
  can be read through the source-directory mount, but changing it does not
  invalidate Reuse. An explicitly declared companion does invalidate on change;
  duplicate/reordered declarations preserve equivalent structure.
- An additional read-only site directory supplied through `APPTAINER_BINDPATH`
  remains accessible alongside gwflow's requested mounts.
- Invalid names and default-name/output collisions fail before managed storage
  initialization. These declaration checks do not require a container runtime.

The mount evidence comes from command reads, attempted source writes, writable
copies, scratch files, and retained results, rather than inspecting generated
Apptainer arguments. The checks preserve ordinary default and deployment mounts;
they do not establish universal confinement against every possible site alias.

The #94 final-commit [CI run](https://github.com/MOMA-AUH/gwflow/actions/runs/36880284278)
passed all 220 tests against the pip-installed package with Apptainer and no
skips. The Conda artifact suite passed with 12 explicit runtime skips, and its
packaged smoke passed.

The #95 checks in [test_container_graphs.py](../tests/test_container_graphs.py)
use installed public workflows and real Apptainer commands to validate:

- Host → container → container → host references, using two distinct SIF paths;
  the fixture contents are identical, and each command reports its selected path.
  Both container steps read staged links and refuse source writes.
- A newly added container consumer reads the producer's retained result after
  eligible producer work cleanup. Only that consumer computes; unchanged runs
  submit nothing and producer work remains absent.
- Image changes refresh every producer target and its consumer, with command
  hashes disabled and equal retained size/mtime. An unrelated Task stays reusable.
  Equivalent aliases reuse; adding/removing image selection refreshes.
- Backend fixtures supply controlled active-job, removed-active-consumer, and
  uncertain-admission observations. Image changes cannot bypass them. Damaged
  ownership also blocks replacement. Explain, detailed status, dry-run, and run
  preserve the prior files and records when replacement is refused.
- A scheduled consumer held after preparation refuses a wrong-generation
  producer Completion record before its command can run. Restoring that evidence
  permits retry without recomputing the producer. Invalid declarations,
  ownership references, and cycles fail before storage initialization.

The #95 final-commit [CI run](https://github.com/MOMA-AUH/gwflow/actions/runs/36883623269)
passed all 229 tests against the pip-installed package with real Apptainer and
no skips. The Conda artifact suite passed with 21 explicit runtime skips, plus
its packaged smoke. Local validation passed the nine new mixed-graph checks and
24 existing staging/graph/dependency regressions.

The #96 additions to [test_staging.py](../tests/test_staging.py) exercise
`stage_as` through public authoring and real container commands:

- Equal input basenames use distinct directories; an explicitly declared data
  and index pair appears together. Commands observe symlinks, original bindings
  resolve to the custom paths, unmapped inputs keep defaults, and obsolete
  default aliases are absent. Nested names, spaces, quotes, and dollar signs work.
- Undeclared references, duplicate logical assignments, invalid relative paths,
  identical normalized destinations, default/override collisions, and ancestry
  conflicts among inputs and with outputs fail before managed initialization.
  A non-mapping value or nonempty mapping on a host target is an authoring error.
- Reordered mappings and explicit default names preserve the existing attempt.
  A changed effective layout refreshes it with command hashes disabled. Explain,
  detailed status and dry-run preserve all managed file bytes and mtimes.
  Controlled active work and malformed ownership block actual replacement too.

The #96 final-commit [CI run](https://github.com/MOMA-AUH/gwflow/actions/runs/36886393557)
passed all 235 tests against the pip-installed package with real Apptainer and
no skips. The Conda artifact suite passed with 24 explicit runtime skips and
its packaged smoke. The local complete staging test file passed all 11 tests.

The #97 checks in [test_container_environment.py](../tests/test_container_environment.py)
run no-input commands in real Apptainer to demonstrate the scratch variants:

- Two commands held concurrently by file gates write the same basename in
  different private TMPDIRs below the configured execution-work root. Managed
  scratch overrides inherited TMPDIR and an explicit Apptainer TMPDIR override.
  Files persist after command exit and cleanup preview, and disappear after
  eligible work deletion. Retained results remain reusable.
- With the opt-out, a job TMPDIR containing spaces is writable and survives work
  cleanup. These tests disable automatic temporary-directory mounts using
  `APPTAINER_NO_MOUNT=tmp`, so gwflow's requested mount supplies the access.
- Image environment values and a literal explicit Apptainer override containing
  dollars/quotes remain usable; ordinary inherited PYTHONPATH is absent inside
  the container. A host target retains its ordinary environment.
- With job TMPDIR unset and the opt-out, the fixture leaves TMPDIR unset and its
  Python uses `/tmp`. Ordinary default mounts remain enabled in this case.
  Both opted-out and managed commands can write a test-owned path under system
  `/tmp`; those files survive eligible work cleanup, while managed scratch does
  not. This demonstrates the documented boundary for commands ignoring TMPDIR.

The #97 final-commit [CI run](https://github.com/MOMA-AUH/gwflow/actions/runs/36888907567)
passed all 240 tests against the pip-installed package with real Apptainer and
no skips. The Conda artifact suite passed with 29 explicit runtime skips plus
packaged smoke. The five new environment checks and existing host TMPDIR check
also passed together locally.

The #98 checks in [test_container_recovery.py](../tests/test_container_recovery.py)
exercise the existing recovery lifecycle with actual container computation:

- One branch succeeds while another writes a partial output and fails. The
  dependent does not run and no retained result appears. Correcting the untracked
  deployment condition permits ordinary same-attempt retry; only the failed
  branch and its dependent compute, using new work and TMPDIR. Abandoned files
  remain owned until eligible cleanup.
- Damaged retained results repair from checked work without repeating commands.
  A deterministic interruption during copying leaves old results intact. Recovery
  retains the attempt and restores the original metadata, even with Apptainer
  unavailable in the finishing job's PATH.
- Unavailable images block repair, partial retry, and transfer continuation without
  changing existing results or evidence. Restoring matching identity restores
  the corresponding decision. Changing image metadata after a preview makes a
  subsequent run refresh the entire Task, including successful siblings.
- Prepared transfer continuation observes images at the frontend. A scheduled
  host finishing operation held at a fixture gate completes even if the image
  becomes unavailable afterward; scheduled operations add no identity recheck.
- Controlled queued/running dependents block retry replacement. Active producer
  work defers repair; active or uncertain consumers, including removed declarations,
  and insufficient ownership prevent repair replacement. Preview and refused-run
  snapshots preserve managed file contents and timestamps.

The launch and output failure cases remain covered by `test_containers.py`:
missing Apptainer, unusable SIF, missing image software, nonzero exits after
writing output, and zero-exit missing/nonregular output sets. The full host graph,
transfer, repair and inspection suites continue to test the shared lifecycle's
additional interruption boundaries. No separate container recovery loop is added.

The #98 final-commit [CI run](https://github.com/MOMA-AUH/gwflow/actions/runs/36891933045)
passed all 247 tests against the pip-installed package with real Apptainer and
no skips. The Conda artifact suite passed with 36 explicit runtime skips and
its packaged smoke. The seven new recovery checks and two host recovery
regressions also passed locally.

The #99 demonstration in [test_example.py](../tests/test_example.py) uses separate
prepared summary/report images and independently installed host factories.
The image build tests check that gwflow is absent and the package command module
loads. The workflow completes A/B, verifies a byte/mtime-preserving cleanup
preview, deletes their work, then adds C via retained references. Only C computes;
its staged `sales/summary.csv` and `returns/summary.csv` symlinks resolve to the
respective retained files. The exact report has rows `apples,17,2,15` and
`pears,8,1,7`; A/B work remains absent, normal logs remain readable, and unchanged
runs submit no jobs. The original installed host scenario remains covered.
See the [public demonstration instructions](../examples/packaged/README.md) for
image preparation and local/Slurm usage. CI prepares all three images for the
installed-package suite; the Conda artifact job explicitly skips runtime cases
while checking the complete host suite and packaged smoke.

The #99 final-commit [CI run](https://github.com/MOMA-AUH/gwflow/actions/runs/36894760576)
passed all 248 tests against the pip-installed package with real Apptainer and
no skips. The Conda artifact suite passed with 37 explicit runtime skips and its
packaged smoke. All four local example tests and the package typecheck passed.

## Repeatable live backend acceptance

[validate_containers.py](../tests/validate_containers.py) reuses the existing
public CLI assertions against actual local workers or Slurm. It does not select
a backend fixture. Prepare/install the three images and distributions as above,
then run from the repository root:

```sh
python tests/validate_containers.py --backend local --root build/validation/local-acceptance
python tests/validate_containers.py --backend slurm --queue short --root build/validation/slurm-acceptance
```

Each root must be new; the Slurm root, installed host environment and all SIFs
must be visible at the same paths on execution nodes. Each Slurm job requests
one CPU, 256 MB and two minutes through public Workflow defaults. Select the
site queue explicitly; `--case NAME` limits a diagnostic run. A wait defaults to
600 seconds and can be changed with `--timeout`. The runner stops at the first
failure, preserves its evidence, and does not automatically cancel jobs. Inspect
any outstanding job IDs before a rerun. Missing images/runtime fail preflight;
skipped tests are not acceptance.

The evidence directory retains generated workflows, managed records and normal
logs, retained results, per-case `transcript.jsonl`, software/image observations
in `environment.json`, and `summary.json`. Transcripts record public commands,
outputs, image path/size/mtime observations, and real Slurm accounting including
IDs, node, state and exit status. The deployment case runs a host target that
reports the actual job's Python, package and Apptainer versions. CI also runs
this harness with local workers after the installed-package suite; its Conda
job continues the artifact installation, host suite and packaged smoke.

The trace-writing mixed/refresh/recovery cases add their test directory through
ordinary `APPTAINER_BINDPATH`, retaining any existing site binds. Those authored
traces and deployment fault files are outside private work; the earlier `/tmp`
harness reached them through Apptainer's default temporary mount. The permission
case adds no such bind: requesting the same source parent both writable through
a site bind and read-only through gwflow is a deployment conflict, not a valid
permission test. All source-write refusal assertions remain in the live cases.

### Deployment observations and limits

The reference run uses installed gwflow `0.4.0` from source commit `ec06a4f`,
gwf `2.1.1`, summary/report distributions `0.3.0`, Python `3.12.14` on Linux,
and Slurm `25.11.6`.
All three SIFs also contain Python `3.12.14`. The basic SIF contains no gwflow
distribution; summary/report SIFs contain only their respective example command
distribution, not gwflow. The runtime is upstream Apptainer `1.5.4-1`, extracted
into the isolated validation directory described above. No image preparation,
package installation or host interpreter injection happens during computation.

The runtime configuration enables proc/sys/dev/devpts/home/tmp mounts, disables
hostfs mounting, and includes `/etc/localtime` and `/etc/hosts` binds. User binds,
overlay and underlay are enabled. Environment cases deliberately supply polluted
host PYTHONPATH and image-value settings, explicit Apptainer overrides, and both
managed and opted-out scratch; those assertions run inside real containers.

Shared BeeGFS directory identity needs particular attention on this deployment.
Frontend `cn-1036` and compute node `cn-1058` report device `48`; `cn-1053` reports
device `49` for the same directory and identical inode. Diagnostic Slurm jobs
`1258516` and `1258523` confirmed those observations. Initial preparation job
`1258408` on `cn-1053` was refused by the existing managed-root identity guard.
No ownership check was weakened and no old records were adopted. Acceptance is
restricted to hosts exposing matching managed-directory identities; this record
does not certify arbitrary BeeGFS clients or heterogeneous mount identities.

The accepted Slurm run uses a validation-only PATH wrapper around the real
`/usr/bin/sbatch` to select `cn-1058` explicitly with its documented
[node-list option](https://slurm.schedmd.com/sbatch.html):

```sh
mkdir -p build/validation/slurm-bin
cat > build/validation/slurm-bin/sbatch <<'SH'
#!/bin/bash
exec /usr/bin/sbatch --nodelist=cn-1058 "$@"
SH
chmod +x build/validation/slurm-bin/sbatch
PATH="$PWD/build/validation/slurm-bin:$PATH" python tests/validate_containers.py \
  --backend slurm --queue short --root build/validation/slurm-acceptance
```

This is site-specific validation setup, not a new backend or a simulated
scheduler. Use nodes compatible with your own deployment. An earlier diagnostic
attempt used an ineffective `SBATCH_NODELIST` environment setting; its two
superseded probe jobs `1258536` and `1258537` were cancelled by the validation
operator. Those attempts are excluded from passing evidence. Actual placements
and terminal states are recorded for the accepted run. Slurm requeued some jobs
before their computation logs appeared, with a delayed next start; intermediate
pending states are not counted as passes. Acceptance uses terminal accounting
and the behavioral assertions after dependencies finish.

Slurm also replaces the submitter's TMPDIR with a node-local job directory.
The first opt-out check incorrectly expected the frontend's temporary path and
failed in job `1258747`, with its dependent `1258748` cancelled. The corrected
Slurm-specific assertion observes the host job environment immediately around
real Apptainer execution using a test-only launch wrapper. Job `1258771` reported
`/tmp/1258771` both on the host and inside the container, and the host read the
container's scratch marker at command exit. Jobs `1258770`–`1258772` completed;
managed cleanup and the unchanged zero-submission run passed. This validates
forwarding of the actual job TMPDIR, including when it differs from the frontend.
Node-local scratch lifetime remains a site responsibility. The local opt-out
test separately proves that gwflow cleanup preserves a shared external scratch
file. The wrapper observes environment and file behavior; it executes the real
runtime with unchanged arguments and propagates its exit status.

### Accepted results

On 2026-10-01, all 13 cases passed on local workers and all 13 passed on real
Slurm. The [runtime record](validation-v0.4.0-runtime.json) preserves exact
software and image observations, each accepted case's job identifiers and
terminal states, both exact packaged reports, and the excluded diagnostics.
The Slurm evidence combines the first nine passed cases, the corrected job-TMPDIR
case, and the three subsequent recovery/packaged cases. Its recorded initial-run
failure is explicitly excluded from the accepted case set.

| Live case | Slurm job evidence | Outcome |
| --- | --- | --- |
| Deployment and image execution | `1258554`–`1258556`, `1258561`–`1258563` | Installed host versions verified; image-only command, scratch cleanup and Reuse passed. |
| Source/work overlap | `1258570`–`1258572` | Readable source, refused writes, writable nested work and TMPDIR. |
| Aliases and companions | `1258594`–`1258596`, `1258608`–`1258610` | Resolved external alias and explicitly named duplicate/companion layouts passed. |
| Mixed execution | `1258614`–`1258622` | Host → image → image → host, checked results and unchanged Reuse. |
| Image refresh | 21 exact IDs in the runtime record, from `1258635` through `1258686` | Whole producer and consumer refresh, independent Task Reuse, zero-submission final run. |
| Environment and scratch | `1258731`–`1258736`, `1258739`–`1258744`, `1258770`–`1258772` | Clean environment, concurrent separate managed scratch, cleanup and actual job-TMPDIR forwarding. |
| Failure and retry | `1258792`–`1258796`, `1258806`–`1258808` | Intentional nonzero container exit refused Completion; successful sibling preserved and retry storage fresh. |
| Unavailable-image repair decision | `1258816`–`1258820`, `1258840`–`1258844` | Missing image blocked without mutation; restoration enabled repair planning, and a later image change refreshed the whole Task. |
| Packaged A/B then C | `1258870`–`1258877`, `1258896`–`1258899` | A/B work deleted; only C computed; exact report rows; A/B work stayed absent and unchanged run submitted nothing. |

The accepted 90 Slurm jobs comprise 87 `COMPLETED`, one intentionally `FAILED`
and its two dependency-cancelled jobs. All cases passed their behavioral
assertions, including recovery from that intended failure. None of those
accepted-case jobs was cancelled by the validation operator. These results are
integration readiness evidence, without a release tag or package publication.

## Parent acceptance matrix

"Real container" below means Apptainer executed authored software. "Live Slurm"
means the scheduler admitted and ran jobs; fixtures are identified separately.

| #92 scenario | Evidence and boundary |
| --- | --- |
| 1. Select an image | Live `image` and `mixed` cases use image-only software without gwflow installed and two selected SIF paths within one Task. Host preparation, output checks and transfer establish retained results. Package images contain independently installed software. |
| 2. Host and mixed execution | Live `mixed` exchanges host/container/host output references. `test_containers.py` checks host execution with Apptainer absent, effective executor rejection including inherited settings, and `test_staging.py` rejects host staging before initialization. The missing-PATH case uses an execution-environment fixture around a real command. |
| 3. Equal basenames and companions | Live `companions` and `packaged` check distinct staged names, symlinks, original bindings, declared index companions and spaces/punctuation. `test_staging.py` checks invalid/default/ancestry collisions, duplicate assignments and effective-layout identity through the installed authoring/CLI boundary. |
| 4. Links and permissions | Live `aliases` and `overlap` read resolved sources, refuse source writes and permit private work/TMPDIR writes, including work beneath a source parent. Focused local tests also cover site binds and declared versus incidental neighbors. |
| 5. Environment and scratch | Live `environment`, `scratch` and `opt_out` check clean environment, explicit overrides, concurrent separate fixed-name files and actual job scratch forwarding. Local opt-out checks additionally verify cleanup non-adoption of external files; Slurm's node-local scratch observation is described above. `test_container_environment.py` also runs real containers with unset opt-out TMPDIR and hardcoded `/tmp` writes. |
| 6. Clean producers, add consumer | Live `packaged` checks preview snapshots, A/B deletion, surviving logs/Completion, C-only work via named retained references, two explicit summary paths, exact report rows and unchanged zero-submission runs. |
| 7. Image changes | Live `refresh` checks multiple images, whole-Task refresh with hashes disabled, consumer invalidation and unrelated Reuse. `test_containers.py` independently changes size, mtime and resolved path, and checks equivalent aliases using actual containers. Backend fixtures control active/uncertain admissions in `test_container_graphs.py`; they are not live scheduler fault injections. |
| 8. Unavailable images and previews | Live `repair` blocks unavailable-image repair without changing snapshots, restores matching identity, previews repair, then refreshes after image change. `test_containers.py` and `test_container_recovery.py` cover unavailable Reuse/retry/continuation and frontend-only observation; deterministic gates hold scheduled host processes while images change. |
| 9. Fail without partial success | Live `retry` writes partial output then exits 7; no retained result or Completion is accepted. Real local tests additionally cover missing Apptainer/software, unusable SIF, and zero-exit missing/nonregular outputs. PATH/launch timing controls are fixture inputs, not simulated Apptainer execution. |
| 10. Retry and repair | Live `retry` preserves its successful sibling and attempt while giving retry new work/TMPDIR. Focused real local recovery tests repair damaged results without recomputation, including interrupted transfer and a finishing job without Apptainer. Active jobs/consumers, uncertain admission and damaged ownership are exercised using deterministic backend/evidence fault inputs. |

All acceptance state is newly created by v0.4. The focused
`test_older_attempt_without_image_evidence_is_rejected_without_deletion` deliberately
removes required evidence and verifies clear refusal with byte/mtime preservation.
It is a rejection test, not a migration or cross-version reuse matrix. Existing
host graph, transfer, storage, inspection and ordinary-gwf tests remain in both
installed-package suites.

Documentation coverage is in the main [README](../README.md): explicit authoring
and executor rules; input naming and tracked companions; requested mounts and
deployment conflicts; clean environment and scratch ownership; launch/output
failure and shared recovery; deployment visibility and image stability; and
frontend pathname/size/mtime limits. The [packaged guide](../examples/packaged/README.md)
provides repeatable authoring, image preparation, local and Slurm commands.

## Limits

This record establishes no broader Apptainer or non-Linux compatibility claim,
and no universal cluster, filesystem, or mount-confinement guarantee. The accepted
mount-identity profile and fixture versus live evidence are stated above.

Image identity is a frontend pathname/size/mtime observation, not a snapshot or
content digest. Deployments keep images stable after submission. Changes
preserving all observed identity fields can remain undetected, and successful
execution does not certify scientific correctness. Unsupported pre-1.0 records
are rejected without migration, adoption, or data deletion.
