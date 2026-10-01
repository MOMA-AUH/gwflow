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
Final live Slurm acceptance remains for the dependent
implementation tickets. This is implementation validation, without
release publication or deployment provisioning.

## Repeatable local checks

Use Python 3.12, gwf 2.1.1, and deployment-provided Apptainer 1.5.4 on Linux.
Install the artifacts and prepare a test image from the repository root:

```sh
python -m pip install .
python -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
mkdir -p build/validation
apptainer build build/validation/basic.sif tests/fixtures/container.def
export GWFLOW_TEST_SIF="$PWD/build/validation/basic.sif"
python -m unittest discover -s tests -p test_containers.py -v
python -m unittest discover -s tests -p test_staging.py -v
python -m unittest discover -s tests -p test_container_graphs.py -v
python -m unittest discover -s tests -p test_container_environment.py -v
python -m unittest discover -s tests -p test_container_recovery.py -v
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

Installed-package validation uses a built wheel with source metadata `0.3.1`
and the new image-aware record requirements, plus summary/report packages
`0.2.0`. This development slice does not change release versions; all test state
is freshly created by the implementation under test.

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

## Limits and remaining evidence

These slices do not establish the packaged container A/B/C demonstration or
live Slurm container acceptance. Those are required by the remaining tickets and
must be recorded before declaring the parent specification complete. The runtime
record establishes no broader Apptainer or non-Linux compatibility claim.

Image identity is a frontend pathname/size/mtime observation, not a snapshot or
content digest. Deployments keep images stable after submission. Changes
preserving all observed identity fields can remain undetected, and successful
execution does not certify scientific correctness. Unsupported pre-1.0 records
are rejected without migration, adoption, or data deletion.
