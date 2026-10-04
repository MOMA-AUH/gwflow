# Registry image validation

This record covers [#122](https://github.com/MOMA-AUH/gwflow/issues/122) and the
registry-image behavior specified by [#117](https://github.com/MOMA-AUH/gwflow/issues/117).
The supported deployment uses Linux, Python 3.12, gwf 2.1.1, Apptainer 1.5.4,
and matching frontend/compute CPU architectures. The image cache, installed host
environment, workflow, and managed storage must be visible at the same absolute
paths on every execution node.

## Repeatable acceptance

Install gwflow and the example task packages, and prepare the three existing
container fixtures as described in the
[container validation instructions](validation-v0.4.0.md#repeatable-local-checks).
Set `GWFLOW_TEST_SIF`, `GWFLOW_TEST_SUMMARY_SIF`, and `GWFLOW_TEST_REPORT_SIF`
to those prepared files and put Apptainer 1.5.4 on `PATH`. These are prerequisites
of the shared acceptance runner; the registry case acquires its own image through
ordinary planning rather than selecting a prepared fixture.

Run from the checkout using new evidence directories:

```console
python -m unittest discover -s tests -p 'test_registry*.py' -v
python tests/validate_containers.py --backend local --case registry --root build/validation/registry-local
python tests/validate_containers.py --backend slurm --queue short --case registry --root build/validation/registry-slurm
```

The local runner starts real local workers. The Slurm runner uses the deployment's
`sbatch` and `sacct`, with one core, 256 MB and two minutes per job on the selected
partition. Set `--queue` for the site. `--timeout` sets the per-command/per-wait
limit, defaulting to 600 seconds; a timeout retains evidence and does not cancel
jobs automatically. Missing prerequisites and skipped cases produce a failure,
not a successful runtime-validation result. Without `--case`, the runner also
executes the existing container acceptance cases.

The [registry example](../examples/registry/workflow.py) selects the same immutable
public source:

```text
docker://docker.io/library/python@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
```

For an ordinary workflow, `GWFLOW_IMAGE_CACHE` selects shared storage; its default
is `~/.cache/gwflow/images`. The acceptance runner isolates its cache under each
evidence root and passes that absolute location to frontend and workers. It does
not require worker registry access. The digest selects the registry source;
local size, resolved path, and nanosecond modification time still govern the
accepted Input baseline. Acquisition of that same source can produce different
local observations and require a fresh Task attempt.

## Behavioral checks

The `registry` case uses the installed public CLI throughout:

1. Cold `status --details` pulls the image on the frontend, while no Task
   attempts, managed work, or retained results exist.
2. Real Apptainer executes the image's Python and retains its message, Python
   version, architecture, and image pathname. A probe records the actual worker
   hostname, architecture, image path, size, time, device, and inode before
   delegating to the deployment's real Apptainer executable.
3. The probe rejects any further `pull`. A forced fresh attempt still executes
   through real Apptainer. Cleanup preserves the cached file; status, explain,
   dry-run, and ordinary run preserve cleaned lifecycle state and submit no jobs.
4. An authored host target holds work after preparation. Changing the image's
   modification time makes the scheduled container target fail its ordinary
   input check before invoking Apptainer. No retained result is installed.
5. A later ordinary run creates a fresh attempt and succeeds using the warm
   cache. The case requires one pull and three actual container executions in
   total, and verifies that final cleanup leaves the cached SIF intact.

The probe observes real processes and disables warm acquisition; it does not
simulate registry responses or container execution. The Slurm case uses no
fixture backend or node-selection wrapper. Independent deterministic tests use
the controlled Apptainer executable to cover unavailable registries, moved tags,
concurrent and interrupted acquisition, cache restoration, retry/repair,
producer/consumer propagation, and activity guards.

## Recorded run

On 2026-10-04, all 32 deterministic registry tests passed in 428.844 seconds.
The real registry acceptance case passed on local workers in 54.686 seconds and
on Slurm in 880.459 seconds, with no skips. Each backend performed exactly one
frontend pull and three real container executions, including warm execution with
pulls disabled and fresh recovery after the expected worker baseline refusal.
The 13 pre-existing live acceptance cases were not rerun in these focused runs.

The [runtime record](validation-registry-runtime.json) preserves the exact
commands, runner/probe source hashes, tool versions, retained outputs, probe
observations, and final Slurm accounting. Production source is based on
`587a49d`; acceptance implementation `86800de` matches the recorded source hashes.
All 21 installed gwflow Python files were compared with the checkout and matched.
The host used Python 3.12.14, gwf 2.1.1, gwflow 0.4.1, and Apptainer 1.5.4-1;
Slurm reported 25.11.6. The image's Python reported 3.12.14. These are validation
versions, without a release selection or package publication.

The frontend was `cn-1039`, and all observed hosts and container executions used
`x86_64`. Slurm used ordinary `/usr/bin/sbatch` and `/usr/bin/sacct` with no node
selection. Its 16 submitted jobs ended as 14 completed, one deliberately failed
container-admission check, and its cancelled dependent completion. A subsequent
fresh attempt completed normally. The cached SIF was 44,408,832 bytes and retained
its path and inode across cleanup and forced attempts. In the Slurm run, the
frontend observed device 49; the first container node, `cn-1048`, observed device
48 for the same image inode `567683900466266639`. Later nodes observed the same
shared image. These host-local device numbers are recorded, not compared as
cross-host identities.

Raw commands, logs and transcripts are under the ignored
`build/validation/registry-runtime/` directory. The committed JSON selects command
results, image observations, and terminal accounting from those raw transcripts.
Its normalization was checked recursively against the selected raw evidence;
only path substitutions and explanatory normalization metadata differ.

The committed runtime record uses `<REPO>` for the checkout root,
`<VALIDATION_ASSETS>` for the separately prepared runtime/fixture directory,
`<USER_WORKSPACE>` for the user's external shared-storage workspace,
`<USER_HOME>` for the user's home, and `<SITE_SOFTWARE>` for the site's software
installation directory. These placeholders preserve path comparisons; standard
system paths such as `/usr/bin/sbatch` remain literal. Device and inode numbers,
timestamps, tool versions, commands, outcomes, and scheduler observations remain
unchanged by normalization.

Published images are never removed by workflow cleanup. User removal remains
uncoordinated and can affect several workflows. Metadata checks do not lock or
snapshot image contents, and changes preserving every observed field can remain
undetected. These acceptance results do not extend the supported scope to private
registries, other transports, worker downloads, architecture overrides, refresh,
eviction, or compatibility migration.
