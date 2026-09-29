"""Three tasks assembled from two independently installed task packages."""

from gwflow import Workflow
from report_tasks import net_report
from summary_tasks import summarize


gwf = Workflow()
gwf.task_from_template(
    "A", summarize("data/sales.csv", "work/A.cleaned.csv", "results/sales.csv")
)
gwf.task_from_template(
    "B", summarize("data/returns.csv", "work/B.cleaned.csv", "results/returns.csv")
)
gwf.task_from_template(
    "C",
    net_report(
        "results/sales.csv", "results/returns.csv",
        "work/C.joined.csv", "results/net.csv",
    ),
)
