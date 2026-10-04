"""Acquire a public registry image during planning, then execute its cached SIF.

Use GWFLOW_IMAGE_CACHE for storage shared by matching-architecture frontend and
compute nodes. Pin a registry digest instead of a tag for reproducible source
selection; existing cache entries are never refreshed automatically.
"""

from gwflow import Task, Workflow


gwf = Workflow()
task = Task(inputs=[])
hello = task.target("hello", inputs=[], outputs=["hello.txt"], image="docker://ubuntu:24.04")
hello << "printf 'hello from the image\\n' > hello.txt"
task.retain("message", source=hello.output("hello.txt"), path="hello.txt")
gwf.task_from_template("registry_example", task)
