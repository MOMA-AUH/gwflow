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

The example packages are version 0.3.0 and require gwflow 0.4.x. Use `-e` for
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
[validation record](../../docs/validation-v0.4.0.md) for tested infrastructure.

## Prepared container images

`container-workflow.py` selects images through ordinary factory arguments:

```python
sales = gwf.task_from_template(
    "A", summarize("data/sales.csv", image="images/summary.sif"), result_dir="sales",
)
returns = gwf.task_from_template(
    "B", summarize("data/returns.csv", image="images/summary.sif"), result_dir="returns",
)
gwf.task_from_template(
    "C", net_report(sales.outputs["summary"], returns.outputs["summary"],
                    image="images/report.sif"), result_dir="net",
)
```

The factories pass the image to each computation target. The report factory's
join target uses `stage_as={"sales/summary.csv": sales,
"returns/summary.csv": returns}` so the identically named retained inputs remain
distinct. Shell bindings still use the original retained references. Relative
image paths resolve from the workflow directory.

On Linux with deployment-provided Apptainer 1.5.4, prepare images from the
repository root after installing the host packages above:

```sh
mkdir -p build/packaged-container-demo/images
cp examples/packaged/container-workflow.py build/packaged-container-demo/workflow.py
cp -r examples/packaged/data build/packaged-container-demo/
apptainer build build/packaged-container-demo/images/summary.sif examples/packaged/images/summary.def
apptainer build build/packaged-container-demo/images/report.sif examples/packaged/images/report.def
cd build/packaged-container-demo
gwf config set use_spec_hashes true
```

Use a fresh demonstration directory for v0.4 state; existing older managed
records are not migrated. Building these images is fixture/deployment setup,
requiring network access to the Python base image and package build requirements.
gwflow itself accepts already prepared local SIF paths and does not acquire them.
Each image provides Bash, Python 3.12, and its independently installed command
package. Image build tests verify that the command module loads without gwflow
installed. Factories need gwflow on the host; their container commands run
`python -m summary_task` or `python -m report_task` using the image interpreter.
Keep the images stable and available throughout the demonstration.

Run the same local-worker sequence above in this fresh directory: complete A/B
with `GWFLOW_EXAMPLE_REPORT=0`, preview `clean-work`, delete eligible work, then
run with C enabled. The preview preserves work, results and records. Deletion
retains checked Completion and normal `gwf logs A__clean --stderr --no-pager`
access. Only C computes, producing the exact CSV shown above; A/B work stays
absent, and a subsequent unchanged run submits no jobs.

For Slurm, put the whole demonstration directory on shared storage visible at
the same paths on compute nodes. Apptainer must be on the job PATH, and the host
Python environment with gwf, gwflow and both factory packages must also be
available there. Set suitable site resources in the copied workflow, for example:

```python
gwf = Workflow(defaults={"queue": "short", "cores": 1,
                         "memory": "256m", "walltime": "00:02:00"})
```

Replace `-b local` with `-b slurm` for run, status and cleanup; do not start local
workers. Wait for the same reusable states before deleting work or adding C.
The queue and resource values are deployment choices, not portable defaults.
Ordinary Apptainer site mounts remain enabled. Final cross-backend observations
are recorded in the linked v0.4 validation record.
