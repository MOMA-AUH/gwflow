"""Two named instances of the imported uppercase task."""

from gwflow import Workflow

from task_library import uppercase

gwf = Workflow()
gwf.task_from_template("alpha", uppercase("alpha"))
gwf.task_from_template("beta", uppercase("beta"))
