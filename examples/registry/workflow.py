"""Acquire a public registry image during planning, then execute its cached SIF.

Use GWFLOW_IMAGE_CACHE for storage shared by matching-architecture frontend and
compute nodes. This digest selects a reproducible registry source; existing
cache entries are never refreshed automatically. See docs/validation-registry.md
for local and Slurm commands, cache behavior, and Input baseline limits.
"""

from gwflow import Task, Workflow, task_template


gwf = Workflow()
@task_template
def registry_example():
    task = Task(inputs=[])
    hello = task.target("hello", inputs=[], outputs=["hello.txt"],
                        image="docker://docker.io/library/python@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e")
    hello << "python -c 'import platform; print(\"hello from Python \" + platform.python_version())' > hello.txt"
    task.retain("message", source=hello.output("hello.txt"), path="hello.txt")
    return task


gwf.task(registry_example())
