"""Public workflow kept unchanged across independently installed environments."""

import os
from pathlib import Path
import shlex

from gwflow import Task, Workflow, shell, task_template


trace = str(Path(__file__).with_name("trace.txt"))
runtime_probe = "python -c " + shlex.quote(
    "import gwflow,gwf,rich,json,sys,platform,importlib.metadata as m; "
    "print(json.dumps(dict(prefix=sys.prefix,package=gwflow.__file__,python=platform.python_version(),"
    "imports={name: module.__file__ for name,module in [('gwflow',gwflow),('gwf',gwf),('rich',rich)]},"
    "versions={name:m.version(name) for name in ('gwflow','gwf','rich')})))"
) + " > {runtime}"


@task_template
def copy_text(source, label):
    task = Task(inputs=[source])
    target = task.target("copy", inputs=[source], outputs=["copy.txt", "runtime.json"])
    target << shell("cat {source} > {out}; " + runtime_probe + "; echo " + shlex.quote(label) + " >> " + shlex.quote(trace),
                    source=source, out=target.output("copy.txt"), runtime=target.output("runtime.json"))
    task.retain("text", source=target.output("copy.txt"), path="text.txt")
    task.retain("runtime", source=target.output("runtime.json"), path="runtime.json")
    return task


@task_template
def concatenate(sources):
    task = Task(inputs=sources)
    target = task.target("join", inputs=sources, outputs=["joined.txt", "runtime.json"])
    target << shell("cat {first} {second} {third} > {out}; " + runtime_probe + "; echo consumer >> " + shlex.quote(trace),
                    first=sources[0], second=sources[1], third=sources[2],
                    out=target.output("joined.txt"), runtime=target.output("runtime.json"))
    task.retain("text", source=target.output("joined.txt"), path="text.txt")
    task.retain("runtime", source=target.output("runtime.json"), path="runtime.json")
    return task


gwf = Workflow()
producers = [gwf.task(copy_text("input.txt", name), alias=name)
             for name in ("completed", "cleaned", "recovered")]
if os.environ.get("GWFLOW_COMPAT_CONSUMER") == "1":
    gwf.task(concatenate([task.outputs["text"] for task in producers]), alias="consumer")
if os.environ.get("GWFLOW_COMPAT_UNOWNED") == "1":
    gwf.task(copy_text("input.txt", "unowned"), alias="unowned")
