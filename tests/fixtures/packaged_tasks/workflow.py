"""Three tasks assembled from two independently installed task packages."""

import os

from gwflow import Workflow
from report_task.templates import net_report
from summary_task.templates import summarize


gwf = Workflow()
sales = gwf.task(summarize("data/sales.csv"), alias="A", result_dir="sales")
returns = gwf.task(summarize("data/returns.csv"), alias="B", result_dir="returns")

# Disable only C to demonstrate adding a consumer after producer cleanup.
if os.environ.get("GWFLOW_EXAMPLE_REPORT", "1") != "0":
    gwf.task(net_report(sales.outputs["summary"], returns.outputs["summary"]), alias="C", result_dir="net")
