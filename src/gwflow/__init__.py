"""Author independent tasks and register them in ordinary gwf workflows."""

from .workflow import Task, Workflow
from .commands import shell

__all__ = ["Task", "Workflow", "shell"]
