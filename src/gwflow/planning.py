"""Shared observations and lifecycle decisions, with no attempt allocation."""

from dataclasses import dataclass

from gwf.backends import BackendStatus, create_backend
from gwf.exceptions import WorkflowError

from . import _files, admission, inputs
from .lifecycle import Store, TaskObservation, declarations, ordered_targets, target_dependencies
from .workflow import lifecycle_jobs


@dataclass
class Plan:
    store: Store
    tasks: list[TaskObservation]


def tracked_commands_match(store, attempt, commands):
    if not attempt["command_tracking"] or commands != attempt["commands"]:
        return False
    return all(store.execution(attempt, local)["command_tracking"]
               and store.execution(attempt, local)["command"] == commands[local] for local in commands)


def retry_targets(store, attempt, jobs):
    replaced = set()
    for local in ordered_targets(attempt["structure"]):
        if jobs[local].state in ("failed", "cancelled"):
            replaced.add(local)
        elif jobs[local].state == "complete":
            try:
                store.checked_target(attempt, local)
            except (WorkflowError, OSError):
                replaced.add(local)
        if replaced.intersection(target_dependencies(attempt["structure"], local)):
            replaced.add(local)
    return [local for local in ordered_targets(attempt["structure"]) if local in replaced]


def retry_observation(store, observation, jobs):
    attempt = observation.attempt
    replaced = retry_targets(store, attempt, jobs)
    if not replaced:
        return False
    store.check_inputs(attempt)
    if not observation.work_present:
        raise WorkflowError("Missing attempt workspace requires a fresh attempt")
    if any(_files.exists(store.attempt_dir(attempt) / filename) for filename in ("manifest.json", "installed.json", "completion.json")):
        raise WorkflowError("Existing results finishing evidence requires recovery before computation retry")
    conflicting = [local for local in [*replaced, "gwflow_complete"] if jobs[local].state == "active"]
    running = [local for local in conflicting if jobs[local].backend_state != BackendStatus.SUBMITTED]
    if running:
        raise WorkflowError("Active dependent work blocks retry: " + ", ".join(running))
    observation.cancel = conflicting
    observation.retry = replaced
    observation.pending = [local for local in ordered_targets(attempt["structure"])
                           if local in replaced or jobs[local].state == "pending"] + ["gwflow_complete"]
    observation.action, observation.reason = "retry", "retry target executions: " + ", ".join(replaced)
    if conflicting:
        observation.reason += "; first confirm cancellation of queued dependents: " + ", ".join(conflicting)
    return True


def plan_workflow(workflow, ctx, *, force=False):
    expected = {f"{name}__{local}" for name, task in workflow._task_declarations.items()
                for local in lifecycle_jobs(task.targets)}
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
                    if store.read(attempt, "ready.json", "ready") is None:
                        raise WorkflowError("Task initialization is not ready; recovery is not yet supported")
                    observation.work_present = _files.exists(store.workspace(attempt))
                    jobs = admission.observe(store, attempt, backend, ctx.backend)
                    observation.submissions = jobs
                    uncertain = [item.submission for item in jobs.values() if item.state == "uncertain"]
                    active = [item.submission for item in jobs.values() if item.state == "active"]
                    pending = [local for local, item in jobs.items() if item.state == "pending"]
                    failed = [local for local, item in jobs.items() if item.state in ("failed", "cancelled")]
                    if uncertain:
                        observation.action, observation.reason = "blocked", "unresolved submission: " + ", ".join(uncertain)
                    elif (force or structure != attempt["structure"] or
                          ctx.config.get("use_spec_hashes") and not tracked_commands_match(store, attempt, commands)):
                        reason = "active work blocks replacement" if active else "changed structure/commands or force requires a fresh attempt; replacement is not yet supported"
                        observation.action, observation.reason = "blocked", reason
                    elif not active and store.read(attempt, "completion.json", "completion", operation=attempt["operation"]) is not None and store.completed(attempt):
                        observation.action, observation.reason = "reuse", "checked Completion and retained metadata match"
                    elif ((jobs["gwflow_prepare"].state in ("failed", "cancelled")
                           or jobs["gwflow_prepare"].backend_state in (BackendStatus.FAILED, BackendStatus.CANCELLED))
                          and not active and observation.work_present
                          and not any(_files.exists(store.execution_dir(attempt, local)) for local in attempt["executions"])):
                        if _files.exists(store.attempt_dir(attempt) / "inputs.json"):
                            store.check_inputs(attempt)
                        observation.action, observation.reason = "prepare", "restart interrupted preparation in the same attempt"
                        observation.pending = lifecycle_jobs(ordered_targets(attempt["structure"]))
                        observation.retry = ordered_targets(attempt["structure"])
                    elif retry_observation(store, observation, jobs):
                        pass
                    elif not failed and pending:
                        if _files.exists(store.attempt_dir(attempt) / "inputs.json"):
                            store.check_inputs(attempt)
                        observation.action, observation.reason = "continue", "continue known submissions without repeating admitted work"
                        observation.pending = pending
                    elif active:
                        observation.action, observation.reason = "active", "queued/running work: " + ", ".join(active)
                    elif store.completed(attempt):
                        observation.action, observation.reason = "reuse", "checked Completion and retained metadata match"
                    else:
                        observation.action, observation.reason = "blocked", "incomplete attempt or damaged results; retry/recovery is not yet supported"
                except (WorkflowError, OSError) as error:
                    observation.action, observation.reason = "blocked", str(error)
            else:
                inputs.observe(structure["inputs"], store.locations)
                observation.pending = lifecycle_jobs(ordered_targets(structure))
            tasks.append(observation)
    return Plan(store, tasks)
