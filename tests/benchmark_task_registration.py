"""Measure authoring independent Tasks through Workflow.task."""

import gc
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
from statistics import median
import tempfile
from time import perf_counter

from gwflow import Task, Workflow, task_template
from gwflow import workflow


@task_template
def sample():
    task = Task(inputs=[])
    write = task.target("write", inputs=[], outputs=["out.txt"])
    write << "touch out.txt"
    task.retain("out", source=write.output("out.txt"), path="out.txt")
    return task


def author(count, root):
    gc.collect()
    start = perf_counter()
    gwf = Workflow(working_dir=root)
    for index in range(count):
        gwf.task(sample(), key=str(index), result_dir=f"samples/{index}/report")
    elapsed = perf_counter() - start
    assert len(gwf.targets) == 3 * count
    return elapsed


if __name__ == "__main__":
    timings = []
    with tempfile.TemporaryDirectory() as root:
        for count in (200, 400, 800):
            seconds = [author(count, root) for _ in range(3)]
            timings.append({"tasks": count, "seconds": seconds, "median_seconds": median(seconds)})
    print(json.dumps({
        "python": platform.python_version(),
        "system": platform.system(),
        "architecture": platform.machine(),
        "gwf": version("gwf"),
        "workflow_sha256": hashlib.sha256(Path(workflow.__file__).read_bytes()).hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "timings": timings,
    }, indent=2))
