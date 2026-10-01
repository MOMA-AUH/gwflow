# Packaged workflow

This example assembles three managed Tasks from two independently installable
Python packages. A and B use `summary_task.summarize(source)` to clean and total
sales and returns. C uses `report_task.net_report(sales, returns)` with the
**named retained references** returned by registering A and B. Factories declare
local outputs and retained mappings; the pipeline chooses each `result_dir`.

| Task | Input | Private intermediate | Retained file |
| --- | --- | --- | --- |
| A | `data/sales.csv` | `cleaned.csv` | `results/sales/summary.csv` |
| B | `data/returns.csv` | `cleaned.csv` | `results/returns/summary.csv` |
| C | A/B `outputs["summary"]` references | `joined.csv` | `results/net/net.csv` |

Each Task contains two computation targets. A and B are independent. C's
preparation waits for checked Completion of both producers, including all their
branches and retained-set installation. Results contain just these three CSVs;
execution identities and bookkeeping live outside results.

From the repository root, install into a Python 3.12 environment containing
pinned gwf 2.1.1:

```sh
python -m pip install .
python -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
cd examples/packaged
gwf config set use_spec_hashes true
```

The example packages are version 0.2.0 and require gwflow 0.3.x. Use `-e` for
editable package development if desired. Package version or implementation
changes have no independent invalidation; changed generated commands are tracked
when hashes are enabled. Use explicit force for otherwise untracked changes.

Start local workers in a second terminal, in this directory and environment:

```sh
gwf -b local workers -n 2
```

Back in the first terminal, complete only A and B. The example's environment
switch omits C's declaration so it can be added later:

```sh
GWFLOW_EXAMPLE_REPORT=0 gwf -b local run
GWFLOW_EXAMPLE_REPORT=0 gwf -b local status
```

Submission returns immediately. Wait until status reports both Tasks as
`reusable` with `work-present`, then preview and delete their eligible work:

```sh
GWFLOW_EXAMPLE_REPORT=0 gwf -b local clean-work
GWFLOW_EXAMPLE_REPORT=0 gwf -b local clean-work --delete
```

Unset `GWFLOW_EXAMPLE_REPORT` if it was exported in your shell. Now add C and
inspect status until it also becomes reusable:

```sh
gwf -b local run
gwf -b local status
cat results/net/net.csv
```

Use `gwf -b local status --details` when debugging to include lifecycle reasons,
attempts, paths, execution submissions, and log locations.

Only C computes, using the producers' retained files while A/B work remains
absent. The exact report is:

```csv
product,sales,returns,net
apples,17,2,15
pears,8,1,7
```

A subsequent unchanged `gwf -b local run` submits no jobs. Starting with C enabled
also works: all Tasks are submitted together with producer dependencies. Keep
results and `.gwf/` for reuse. Cleanup gives up intermediate retry/repair sources;
if retained results later become damaged, cleaned producers must compute afresh.

For Slurm, use the configured backend and shared storage visible to compute nodes.
Site resources belong in Workflow defaults; gwf 2.1.1 calls the partition option
`queue`. See the [managed lifecycle documentation](../../README.md) for storage
placement, force, repair, uncertainty and operational assumptions, and the
[validation record](../../docs/validation-v0.3.0.md) for tested infrastructure.
