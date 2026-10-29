"""Evidence categories carried by Task inspection failures."""

from gwf.exceptions import WorkflowError


class InspectionBlocker(WorkflowError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
