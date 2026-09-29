# Packaged transactions workflow

This example assembles three gwflow tasks from two installable Python packages.
Tasks **A** and **B** are separate instances of the `summary_tasks.summarize`
factory. They clean and total sales and returns independently. Task **C** comes
from `report_tasks.net_report` and calculates net sales from their retained
outputs.

| Task | External input | Internal intermediate | Retained output |
| --- | --- | --- | --- |
| A | `data/sales.csv` | `work/A.cleaned.csv` | `results/sales.csv` |
| B | `data/returns.csv` | `work/B.cleaned.csv` | `results/returns.csv` |
| C | Both summaries | `work/C.joined.csv` | `results/net.csv` |

Each task has two ordinary gwf targets. A and B can run concurrently; C waits
for both tasks to complete. The package source trees are separate from the
pipeline, and each package has its own `pyproject.toml` and version. The packages
are kept in this repository for convenience; they do not need separate GitHub
repositories.

## Run locally

First activate a Python 3.12 environment with gwflow and gwf 2.1.1 installed,
as described in the [project README](../../README.md#install). From this
directory, install the two task packages:

```bash
python -m pip install -e packages/summary-tasks
python -m pip install -e packages/report-tasks
```

Editable installs pick up changes to package source during development. To test
the installation behavior used for a release, omit `-e`. Both packages install
into the same environment as gwflow.

Start local workers in one terminal:

```bash
gwf -b local workers -n 2
```

Then, in another terminal in this directory, run the workflow and inspect its
retained report:

```bash
gwf -b local run
gwf -b local status
cat results/net.csv
```

The final report contains `apples,17,2,15` and `pears,8,1,7` under the
`product,sales,returns,net` header. After all three tasks have completed,
delete only their intermediates and run again:

```bash
rm work/A.cleaned.csv work/B.cleaned.csv work/C.joined.csv
gwf -b local run
```

The second run submits no targets and does not recreate the deleted files.
Keep `.gwf/`: it contains the completion records needed for reuse. Inputs and
retained outputs remain in place.
