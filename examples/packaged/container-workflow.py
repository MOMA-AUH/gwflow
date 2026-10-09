"""Prepared images selected explicitly by independently installed Task factories."""

import os

from gwflow import Workflow
from report_task.templates import net_report
from summary_task.templates import summarize


gwf = Workflow()
sales = gwf.task(summarize("data/sales.csv", image="images/summary.sif"), alias="A", result_dir="sales")
returns = gwf.task(summarize("data/returns.csv", image="images/summary.sif"), alias="B", result_dir="returns")

if os.environ.get("GWFLOW_EXAMPLE_REPORT", "1") != "0":
    gwf.task(net_report(sales.outputs["summary"], returns.outputs["summary"],
                                         image="images/report.sif"), alias="C", result_dir="net")
