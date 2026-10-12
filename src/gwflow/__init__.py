"""Author independent Tasks and register them in managed workflows."""

from .workflow import Task, Workflow, task_template
from .commands import shell

__all__ = ["Task", "Workflow", "shell", "task_template"]
