"""Three tasks assembled from two independently installed task packages."""

import os

from gwflow import Workflow
from report_task import net_report
from summary_task import summarize


gwf = Workflow()
sales = gwf.task_from_template("A", summarize("data/sales.csv"), result_dir="sales")
returns = gwf.task_from_template("B", summarize("data/returns.csv"), result_dir="returns")

# Disable only C to demonstrate adding a consumer after producer cleanup.
if os.environ.get("GWFLOW_EXAMPLE_REPORT", "1") != "0":
    gwf.task_from_template("C", net_report(sales.outputs["summary"], returns.outputs["summary"]), result_dir="net")
