# Packaged workflow

This example assembles three managed Tasks from two independently installable
Python packages. A and B use `summary_task.templates.summarize(source)` to clean
and total sales and returns. C uses `report_task.templates.net_report(sales,
returns)` with A and B's named retained references. Factories define work and
retained files; the pipeline chooses each `result_dir`.

| Task | Input | Private intermediate | Retained file |
| --- | --- | --- | --- |
| A | `data/sales.csv` | `cleaned.csv` | `results/sales/summary.csv` |
| B | `data/returns.csv` | `cleaned.csv` | `results/returns/summary.csv` |
| C | A/B `outputs["summary"]` | `joined.csv` | `results/net/net.csv` |

Each Task has two computation targets. A and B are independent. C's preparation
waits for checked Completion of both producers, including their full retained
sets. Results contain only the three CSVs; bookkeeping stays outside results.

## Host execution

From this repository checkout, install into a Python 3.12 environment:

```sh
python -m pip install .
python -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
cd examples/packaged
```

The factory packages require gwflow `>=0.9.0,<0.10`; the commands below use the
current checkout's dedicated `gwflow` CLI. Editable installs with `-e` are also
supported. Package implementation or version changes do not independently
invalidate Reuse; use force for changes not reflected in tracked commands,
structure, inputs, or images.
Command tracking is mandatory and needs no configuration.

Start workers in a second terminal with the same environment and directory:

```sh
gwflow -b local workers -n 2
```

In the first terminal, select only A and B:

```sh
gwflow -b local run A B --dry-run
gwflow -b local run A B
gwflow -b local status A B
```

Submission is asynchronous. Repeat status until both Tasks are `reusable`, then
preview and delete their eligible work:

```sh
gwflow -b local clean A B
gwflow -b local clean A B --delete
```

Now select C. Its preview includes reusable A/B dependencies, while its status
shows C alone:

```sh
gwflow -b local run C --dry-run
gwflow -b local run C
gwflow -b local status C
```

Wait for C to become reusable, then run `cat results/net/net.csv`:

```csv
product,sales,returns,net
apples,17,2,15
pears,8,1,7
```

Only C computes, using retained files while A/B work remains absent. An unchanged
`gwflow -b local run` submits no new jobs. An initial run without selectors also
works, submitting all three Tasks with producer dependencies.

Use `gwflow -b local status C --details` for job states,
`gwflow -b local run C --dry-run --details` for reasons, attempts and paths, and
`gwflow -b local logs C__join --stderr --no-pager` for an individual job log.
Keep results and `.gwf/` together. Cleanup gives up retry/repair sources; damaged
results require fresh computation once the corresponding work has been cleaned.
Stop local workers with Ctrl-C when finished.

To try adding a consumer declaration later, run the producer commands with
`GWFLOW_EXAMPLE_REPORT=0`, then unset that variable before selecting C.
The [release smoke check](../../tests/release_smoke.py) exercises this extension
through the installed package and the same Task selectors.

## Prepared container images

[`container-workflow.py`](container-workflow.py) supplies `image="images/summary.sif"`
to the summary factories and `image="images/report.sif"` to the report factory.
The join target uses `stage_as={"sales/summary.csv": sales,
"returns/summary.csv": returns}` to distinguish inputs with the same basename.
Shell bindings still use the original retained references.

On Linux with deployment-provided Apptainer 1.5.4, prepare images from the
repository root after installing the host packages above:

```sh
mkdir -p build/packaged-container-demo/images
cp examples/packaged/container-workflow.py build/packaged-container-demo/workflow.py
cp -r examples/packaged/data build/packaged-container-demo/
apptainer build build/packaged-container-demo/images/summary.sif examples/packaged/images/summary.def
apptainer build build/packaged-container-demo/images/report.sif examples/packaged/images/report.def
cd build/packaged-container-demo
```

Use a fresh demonstration directory. Image builds need network access for their
base image and build dependencies; gwflow uses the prepared local SIF paths.
Each image provides Bash, Python 3.12, and its command package. Image build tests
check that the command module loads without gwflow in the image. Factories use
gwflow on the host; commands run `python -m summary_task` or
`python -m report_task` with the image's interpreter. Keep images available and
unchanged throughout execution.

Start workers in this directory and follow the same A/B → cleanup → C sequence.
Cleanup preserves results, checked Completion, and logs such as
`gwflow -b local logs A__clean --stderr --no-pager`. The output CSV and subsequent
Reuse are the same as for host execution.

## Slurm and further guidance

For Slurm, keep the workflow, installed Python environment, managed storage, and
images visible at the same absolute paths on frontend and compute nodes.
Apptainer must be on the job PATH for container commands. Choose site resources
in Workflow defaults; gwf 2.1.1 calls the partition option `queue`, for example:

```python
gwf = Workflow(defaults={"queue": "short", "cores": 1,
                         "memory": "256m", "walltime": "00:02:00"})
```

Replace `-b local` with `-b slurm` for run, status, logs, and cleanup, and do not
start local workers. Queue and resource values are site choices. Wait for the
same reusable states before cleanup. Ordinary Apptainer site mounts remain
enabled.

See the [reference guide](../../docs/reference.md) for storage, force, Repair,
uncertainty, and container details, and the [validation guide](../../docs/validation/README.md)
for current checks and version-specific historical evidence.
