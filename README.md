# gwflow

[![Conda Version](https://img.shields.io/conda/vn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow) [![Conda Downloads](https://img.shields.io/conda/dn/MOMA-AUH/gwflow?style=for-the-badge&cacheSeconds=300)](https://anaconda.org/MOMA-AUH/gwflow)

gwflow runs workflows as reusable Tasks: groups of jobs with named results.
Computation uses disposable work directories. Retained results stay available
after that work is cleaned. It uses [gwf](https://gwf.app/) for scheduling.

## Install

Use Linux and Bash. The recommended installation is the released Conda package
from the [MOMA-AUH channel](https://anaconda.org/MOMA-AUH/gwflow).
Conda installs Python 3.12 and the official gwf 3.0.0 package from `gwforg`
as dependencies. The example also needs deployment-provided Apptainer 1.5.4
on `PATH` and a Linux x86_64 host.

Create and activate the environment:

```sh
conda create -n gwflow --override-channels -c MOMA-AUH -c gwforg -c conda-forge gwflow
conda activate gwflow
gwf --version
gwflow --version
apptainer --version
```

For release details, see the [release notes](docs/history.md).
For development from source, use the [validation guide](docs/validation/README.md).

## Run the example

Clone the release that matches `gwflow --version`, then enter its example directory.
For v0.11.0:

```sh
git clone --branch v0.11.0 https://github.com/MOMA-AUH/gwflow.git
cd gwflow/example
```

If you already have the matching checkout, enter its `example/` directory.
All input data is synthetic and included. Apptainer downloads the public Bioconda
container images on first use.

The [workflow](example/workflow.py) aligns reads from two samples and produces a
combined report. A read is a short DNA sequence. Each sample uses one Task
with four computation targets: align, sort, index, and statistics.
The report Task consumes the retained statistics from both samples.

In the first terminal, select the local backend:

```sh
gwflow config set backend local
```

In a second terminal, activate the same environment and enter the same
`gwflow/example` directory. Start the local scheduler with a two-core budget:

```sh
conda activate gwflow
cd gwflow/example
gwflow workers -n 2 --max-memory 4g
```

Keep the scheduler running. `-n 2` budgets requested cores, so one two-core job
uses the entire core budget. Each workflow directory has its own scheduler and
independent budgets. Divide machine capacity across concurrently running workflows.
Memory reservations do not enforce actual memory use.

Local jobs default to one hour, including preparation and completion jobs.
See [local resources and walltime](docs/reference.md#local-resources-and-walltime)
for overrides and worker requirements.

Back in the first terminal, run the sample Tasks:

```sh
gwflow run 'align__*' --dry-run
gwflow run 'align__*'
gwflow wait 'align__*' --interval 1
gwflow status 'align__*' --static
```

The first preview downloads missing images into the shared image cache.
Submission returns before the jobs finish. `wait` observes submitted work until
it completes or needs another action. Continue when both sample Tasks are `reusable`.

Preview cleanup, then remove their disposable work:

```sh
gwflow clean 'align__*'
gwflow clean 'align__*' --delete
cat results/sample_a/flagstat.txt
```

The alignment intermediates are gone. The retained alignments, indexes, and
statistics remain. Run the report from those retained statistics:

```sh
gwflow run report --dry-run
gwflow run report
gwflow wait report --interval 1
cat results/report/summary.tsv
gwflow run
```

The report preview shows Reuse for both sample Tasks. Only the report computes,
and the final unchanged run submits no new jobs. Open
`results/report/report.html` in a browser to compare the samples.
The [example guide](example/README.md) explains the data, targets, outputs, and container images.

## Everyday commands

Run without selectors to consider the whole workflow. Exact Task names and quoted
patterns select whole Tasks. Every selector must match:

```sh
gwflow run                         # run all Tasks with their dependencies
gwflow run align__sample_a --dry-run
gwflow status                      # interactive view in a capable terminal
gwflow status --static             # print one report
gwflow status align__sample_a --details
gwflow wait report --interval 1 --timeout 300
gwflow logs align__sample_a        # list current jobs and available logs
gwflow logs align__sample_a__align --stderr --no-pager
gwflow logs align__sample_a --history
```

A selected run includes required upstream Tasks and reuses valid results.
`wait` observes existing work. It does not submit or retry jobs.
Its default inspection interval is 60 seconds, with no time limit.
A timeout stops waiting but leaves submitted jobs running.
See [waiting for completion](docs/reference.md#waiting-for-completion) for exit codes and recovery behavior.

To stop a Task's jobs, preview with `gwflow cancel align__sample_a --dry-run`.
Then request cancellation with `gwflow cancel align__sample_a`.
Wait for the jobs to stop before retrying with `gwflow run align__sample_a`.
Cancellation preserves completed work and retained results.

Command changes trigger a fresh Task attempt automatically, even after cleanup.
For environment changes that leave commands, inputs, and images unchanged,
request a fresh attempt explicitly:

```sh
gwflow run align__sample_a --force --dry-run
gwflow run align__sample_a --force
gwflow wait align__sample_a --interval 1
```

Force applies only to directly selected Tasks. After a producer changes, run
`gwflow run report` to update its downstream report.
Keep `results/` and `.gwf/` together. Cleanup preserves retained results, logs,
and the records needed for Reuse. Damaged results require fresh computation
when their Task's work is already cleaned.

Stop the local workers with Ctrl-C when finished. Use `gwflow --help` and
`gwflow COMMAND --help` for all options. Managed workflows use `gwflow`.
Ordinary `gwf.Workflow` workflows continue to use `gwf`.

## More

- [Bioinformatics example and synthetic data](example/README.md)
- [Operating, upgrading, and recovering workflows](docs/operating.md)
- [Task authoring and dependencies](docs/reference.md#authoring-tasks)
- [Selection, status, logs, and backend configuration](docs/reference.md#commands-and-reports)
- [Container images and staged inputs](docs/reference.md#containers)
- [Lifecycle, recovery, cleanup, and storage](docs/reference.md#lifecycle-and-recovery)
