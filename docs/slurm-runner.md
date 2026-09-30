# Self-hosted Slurm acceptance runner

Issue #36 tests a Conda candidate on the intended `www.genome.au.dk` HPC. The
repository is public, so this runner is used only by the manually dispatched
`Slurm acceptance` workflow and the trusted tag release workflow. It is never
selected by `pull_request`. The existing GitHub-hosted local-backend CI remains
the PR gate.

## Site configuration

| Setting | Value or requirement |
| --- | --- |
| Runner labels | `self-hosted`, `linux`, `gwflow-slurm` |
| Slurm account and partition | Site defaults; no `account` or `queue` override |
| Shared storage | Set repository Actions variable `SLURM_SHARED_ROOT` to an absolute path writable by the runner and compute jobs; the candidate Conda prefix, workflows, and evidence live beneath it |
| Slurm policy | `DependencyParameters=kill_invalid_depend` must cancel `afterok` dependents of failed jobs; `sacct` accounting must be available |
| Runner software | `conda`, `sbatch`, `squeue`, `sacct`, `scontrol`, `bash`, `timeout`, and outbound access to GitHub and the `gwforg`/`conda-forge` channels |
| Deadlines | `SLURM_WAIT_SECONDS` repository variable defaults to 2400 seconds; Actions job limit is 90 minutes; each gated compute job has a 600-second safety timeout |

A read-only preflight on 2026-09-30 from the HPC returned Slurm 25.11.6,
cluster `genomedk`, default partition `normal`, accounting via `slurmdbd`,
and `DependencyParameters=kill_invalid_depend`. The runner repeats the policy
check and records its own site observations when acceptance runs.

Use a persistent frontend or service host approved for GitHub runner processes
and Slurm submissions. The service account must have permission to submit small
jobs and read their accounting records. Ensure the chosen shared root and the
installed Conda prefix are visible at the same absolute path on compute nodes.
The acceptance run writes synthetic files only, under
`$SLURM_SHARED_ROOT/gwflow-acceptance/<run-id>-<attempt>/`. Keep that directory
after a failed run for diagnosis; remove old runs under the site's normal
retention policy.

## Register the runner

1. On the intended host, confirm `command -v conda sbatch squeue sacct scontrol`
   succeeds, and confirm `scontrol show config` and a small permitted `sbatch`
   submission work with the default account and partition. Check
   `scontrol show config | grep DependencyParameters` and `sacct` visibility. Obtain
   site approval if a persistent runner is not permitted on that host.
2. Open [repository Settings → Actions → Runners](https://github.com/MOMA-AUH/gwflow/settings/actions/runners)
   and choose **New self-hosted runner**. Select Linux and the host architecture.
   Run the download and checksum commands shown there; the version and
   registration token are time dependent. Keep the runner installation outside
   the shared test output directory.
3. Run the page's `./config.sh` command with `--labels gwflow-slurm`, or add
   `gwflow-slurm` on the runner's labels page after registration. Use a unique
   runner name. Start it with `./run.sh` for a first check; use the site's
   approved long-running service method for ongoing operation. GitHub documents
   [registration](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners),
   [labels](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/apply-labels),
   and [Linux service setup](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application?platform=linux).
4. Choose a shared directory and set the repository Actions variable:

   ```bash
   gh variable set SLURM_SHARED_ROOT --repo MOMA-AUH/gwflow --body '/absolute/shared/path'
   ```

   If queue delays require longer waits, set `SLURM_WAIT_SECONDS` in the same
   way. No account or partition variable is needed at this site.
5. After this PR is merged, run `gh workflow run slurm-acceptance.yml --repo
   MOMA-AUH/gwflow --ref master` or use **Actions → Slurm acceptance → Run
   workflow**. Check that the self-hosted job is picked up and download its
   `gwflow-slurm-evidence-*` artifact. A release tag builds and tests the exact
   candidate Conda file before `publish` can upload it.

The runner uses the candidate installed on shared storage. Its test records
the candidate SHA-256, package path, Python/gwf versions, runner name, selected
Slurm configuration, target-to-job-ID map, queue and accounting observations,
CLI transcripts, node probes, results, and `.gwf/logs`. Passing acceptance
supports the tested HPC, package, and configuration only. Runner or cluster
unavailability leaves the release job waiting or failed, so publication is
delayed. Do not publish from a tag until this gate passes.
