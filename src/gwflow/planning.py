"""Shared observations and lifecycle decisions, with no attempt allocation."""

from dataclasses import dataclass

from gwf import Target
from gwf.backends import BackendStatus, create_backend
from gwf.exceptions import WorkflowError

from . import _files, inputs
from .lifecycle import Store, TaskObservation, declarations


@dataclass
class Plan:
    store: Store
    tasks: list[TaskObservation]


def plan_workflow(workflow, ctx, *, force=False):
    expected = {f"{name}__{local}" for name, task in workflow._task_declarations.items()
                for local in [*task.targets, "gwflow_prepare", "gwflow_complete"]}
    if set(workflow.targets) != expected:
        raise WorkflowError("Every computation target in gwflow.Workflow must belong to a registered Task")
    store = Store.for_workflow(workflow)
    declared = {name: declarations(task, store) for name, task in workflow._task_declarations.items()}
    tasks = []
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        for name, (structure, commands) in declared.items():
            attempt = store.current(name, workflow._result_dirs[name])
            observation = TaskObservation(name, "fresh", "no completed managed attempt", structure, commands, attempt)
            if attempt is not None:
                try:
                    observation.work_present = _files.exists(store.workspace(attempt))
                    active, uncertain, states = [], [], {}
                    acknowledged = True
                    for local, job in attempt["jobs"].items():
                        identity = store.job_identity(attempt, local)
                        intent = store.read(attempt, f"submissions/{local}-intent.json", "submission-intent", **identity)
                        ack = store.read(attempt, f"submissions/{local}-ack.json", "submission-ack", **identity)
                        if intent is not None and (ack is None or "job_id" not in ack):
                            uncertain.append(job)
                        target = Target(job, [], [], {})
                        tracked = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
                        acknowledged &= ack is not None and tracked is not None and ack.get("job_id") == tracked
                        state = backend.status(target)
                        states[local] = state
                        if state in (BackendStatus.SUBMITTED, BackendStatus.RUNNING):
                            active.append(job)
                    if uncertain:
                        observation.action, observation.reason = "blocked", "unresolved submission: " + ", ".join(uncertain)
                    elif active:
                        observation.action, observation.reason = "active", "queued/running work: " + ", ".join(active)
                    elif force:
                        observation.action, observation.reason = "blocked", "fresh-attempt replacement is not yet supported"
                    elif structure != attempt["structure"]:
                        observation.action, observation.reason = "blocked", "changed structure requires a fresh attempt; replacement is not yet supported"
                    elif ctx.config.get("use_spec_hashes") and (not attempt.get("command_tracking") or commands != attempt["commands"]):
                        observation.action, observation.reason = "blocked", "changed or unrecorded commands require a fresh attempt; replacement is not yet supported"
                    elif (acknowledged and states["gwflow_prepare"] in (BackendStatus.FAILED, BackendStatus.CANCELLED)
                          and all(state in (BackendStatus.COMPLETED, BackendStatus.FAILED, BackendStatus.CANCELLED)
                                  for state in states.values())
                          and observation.work_present
                          and not any(_files.exists(store.execution_dir(attempt, local)) for local in attempt["executions"])):
                        if _files.exists(store.attempt_dir(attempt) / "inputs.json"):
                            store.check_inputs(attempt)
                        observation.action, observation.reason = "prepare", "restart interrupted preparation in the same attempt"
                    elif store.completed(attempt):
                        observation.action, observation.reason = "reuse", "checked Completion and retained metadata match"
                    else:
                        observation.action, observation.reason = "blocked", "incomplete attempt or damaged results; retry/recovery is not yet supported"
                except (WorkflowError, OSError) as error:
                    observation.action, observation.reason = "blocked", str(error)
            else:
                inputs.observe(structure["inputs"], store.locations)
            tasks.append(observation)
    return Plan(store, tasks)
