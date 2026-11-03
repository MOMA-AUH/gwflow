"""Two named instances of the imported uppercase task."""

from gwflow import Workflow

from task_library import uppercase

gwf = Workflow()
gwf.task(uppercase("input.txt"), alias="alpha")
gwf.task(uppercase("input.txt"), alias="beta")
