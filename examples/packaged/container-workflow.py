"""Prepared images selected explicitly by independently installed Task factories."""

import os

from gwflow import Workflow
from report_task import net_report
from summary_task import summarize


gwf = Workflow()
sales = gwf.task_from_template("A", summarize("data/sales.csv", image="images/summary.sif"), result_dir="sales")
returns = gwf.task_from_template("B", summarize("data/returns.csv", image="images/summary.sif"), result_dir="returns")

if os.environ.get("GWFLOW_EXAMPLE_REPORT", "1") != "0":
    gwf.task_from_template("C", net_report(sales.outputs["summary"], returns.outputs["summary"],
                                         image="images/report.sif"), result_dir="net")
