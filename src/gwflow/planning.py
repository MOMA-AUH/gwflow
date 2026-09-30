"""Shared observations and lifecycle decisions, with no attempt allocation."""

from dataclasses import dataclass

from gwf.backends import BackendStatus, create_backend
from gwf.exceptions import WorkflowError

from . import _files, admission, inputs, transfer
from .lifecycle import Store, TaskObservation, declarations, ordered_tasks, ordered_targets, producer_names, target_dependencies
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
    if any(_files.exists(store.record_path(attempt, filename)) for filename in ("manifest.json", "installed.json", "completion.json")):
        raise WorkflowError("Existing results finishing evidence requires recovery before computation retry")
    admission.require_retry_inactive(jobs, replaced)
    observation.retry = replaced
    observation.pending = [local for local in ordered_targets(attempt["structure"])
                           if local in replaced or jobs[local].state == "pending"]
    observation.action, observation.reason = "retry", "retry target executions: " + ", ".join(replaced)
    if jobs["gwflow_complete"].state == "active":
        observation.reason += "; finishing deferred until its previous queued submission settles; run again afterward"
    else:
        observation.pending.append("gwflow_complete")
    return True


def pending_submissions(attempt, jobs):
    pending = [local for local, item in jobs.items() if item.state == "pending"]
    finishing = jobs["gwflow_complete"]
    if (finishing.state in ("failed", "cancelled") and finishing.intent is not None
            and not admission.dependencies_current(attempt, finishing.intent)):
        pending.append("gwflow_complete")
    return pending


def fresh_observation(store, observation, reason):
    active = [item.submission for item in observation.submissions.values() if item.state in ("active", "uncertain")]
    if active:
        raise WorkflowError("Unresolved or active work blocks replacement: " + ", ".join(active))
    inputs.observe([value for value in observation.structure["inputs"] if isinstance(value, str)], store.locations)
    observation.removal = store.result_removal(observation.attempt) if observation.attempt else None
    observation.action, observation.reason = "fresh", reason
    if observation.removal is not None:
        observation.reason += f"; remove previous results at {store.result_dir(observation.attempt)} before submission"
    observation.pending = lifecycle_jobs(ordered_targets(observation.structure))


def input_metadata_changed(store, attempt):
    if not _files.exists(store.attempt_dir(attempt) / "inputs.json"):
        return False
    baseline = store.input_baseline(attempt)
    try:
        observed = store.observe_inputs(attempt)
    except WorkflowError as error:
        raise WorkflowError(f"Inputs unavailable; a fresh attempt requires valid inputs: {error}") from error
    return baseline["inputs"] != observed


def recover_transfer(store, observation, jobs):
    try:
        recovery = transfer.inspect(store, observation.attempt)
    except transfer.InvalidSources:
        attempt = observation.attempt
        if (observation.work_present and store.repair_intent(attempt) is None
                and not any(_files.exists(store.record_path(attempt, filename))
                            for filename in ("manifest.json", "installed.json", "completion.json"))
                and retry_observation(store, observation, jobs)):
            return
        fresh_observation(store, observation, "transfer sources cannot be recovered; fresh computation is required")
        return
    observation.action, observation.reason = "transfer", recovery.reason
    observation.pending = ["gwflow_complete"]


def completed_observation(store, observation):
    try:
        completed = store.completed(observation.attempt)
    except (WorkflowError, OSError):
        completed = False
    if completed:
        observation.action, observation.reason = "reuse", "checked Completion and retained metadata match"
        return
    try:
        transfer.repair_sources(store, observation.attempt)
    except transfer.InvalidSources:
        fresh_observation(store, observation, "retained repair sources are unavailable or invalid; fresh computation is required")
        return
    observation.action, observation.reason = "repair", "restore damaged retained results from checked work under the same attempt"
    observation.pending = ["gwflow_complete"]


def awaiting_producer_metadata(store, observation):
    if observation.action in ("blocked", "deferred", "repair", "transfer"):
        return True
    attempt = observation.attempt
    return (observation.action == "active" and store.repair_intent(attempt) is not None
            and store.read(attempt, "completion.json", "completion", operation=attempt["operation"]) is None)


def plan_workflow(workflow, ctx, *, force=False, force_tasks=()):
    if force and force_tasks:
        raise WorkflowError("Cannot combine --force and --force-task")
    unknown = set(force_tasks) - workflow._task_declarations.keys()
    if unknown:
        raise WorkflowError("Unknown Task names for --force-task: " + ", ".join(sorted(unknown)))
    expected = {f"{name}__{local}" for name, task in workflow._task_declarations.items()
                for local in lifecycle_jobs(task.targets)}
    if set(workflow.targets) != expected:
        raise WorkflowError("Every computation target in gwflow.Workflow must belong to a registered Task")
    store = Store.for_workflow(workflow)
    declared = {name: declarations(task, store, workflow) for name, task in workflow._task_declarations.items()}
    tasks = []
    selected = {}
    with create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
        for name in ordered_tasks(declared):
            structure, commands = declared[name]
            attempt = store.current(name, workflow._result_dirs[name])
            observation = TaskObservation(name, "fresh", "no completed managed attempt", structure, commands, attempt)
            if attempt is not None:
                try:
                    initializing = store.read(attempt, "ready.json", "ready") is None
                    if initializing:
                        store.initialization(attempt)
                        if _files.exists(store.attempt_dir(attempt) / "admissions"):
                            raise WorkflowError("Task initialization is not ready but has submission evidence")
                    observation.work_present = _files.exists(store.workspace(attempt))
                    jobs = admission.observe(store, attempt, backend, ctx.backend)
                    finishing = jobs["gwflow_complete"]
                    observation.submissions = jobs
                    uncertain = [item.submission for item in jobs.values() if item.state == "uncertain"]
                    active = [item.submission for item in jobs.values() if item.state == "active"]
                    pending = pending_submissions(attempt, jobs)
                    failed = [local for local, item in jobs.items() if item.state in ("failed", "cancelled") and local not in pending]
                    if uncertain:
                        observation.action, observation.reason = "blocked", "unresolved submission: " + ", ".join(uncertain)
                    elif (force or name in force_tasks or structure != attempt["structure"] or
                          any(selected[producer].action == "fresh" or selected[producer].attempt["attempt"] != attempt["producers"].get(producer)
                              for producer in producer_names(structure)) or
                          ctx.config.get("use_spec_hashes") and
                          (commands != attempt["commands"] or not attempt["command_tracking"] or
                           not initializing and not tracked_commands_match(store, attempt, commands))):
                        fresh_observation(store, observation, "force or changed structure, commands, or producer identity requires a fresh attempt")
                    elif initializing:
                        observation.removal = store.result_removal(attempt)
                        observation.action, observation.reason = "initialize", "resume selected initialization; remove previous results before submission"
                        observation.pending = lifecycle_jobs(ordered_targets(structure))
                    elif not active and any(awaiting_producer_metadata(store, selected[producer])
                                            for producer in producer_names(structure)) and _files.exists(store.attempt_dir(attempt) / "inputs.json"):
                        observation.action, observation.reason = "deferred", "producer results need recovery; a later invocation must replan input validity after restored metadata is available"
                    elif input_metadata_changed(store, attempt):
                        fresh_observation(store, observation, "input metadata changed; a fresh attempt is required")
                    elif not active and store.read(attempt, "completion.json", "completion", operation=attempt["operation"]) is not None:
                        completed_observation(store, observation)
                    elif not active and store.cleanup_record(attempt) is not None:
                        completed_observation(store, observation)
                    elif not active and store.repair_intent(attempt) is not None:
                        recover_transfer(store, observation, jobs)
                    elif ((jobs["gwflow_prepare"].state in ("failed", "cancelled")
                           or jobs["gwflow_prepare"].backend_state in (BackendStatus.FAILED, BackendStatus.CANCELLED))
                          and not active and observation.work_present
                          and not any(_files.exists(store.execution_dir(attempt, local)) for local in attempt["executions"])):
                        if _files.exists(store.attempt_dir(attempt) / "inputs.json"):
                            store.check_inputs(attempt)
                        observation.action, observation.reason = "prepare", "restart interrupted preparation in the same attempt"
                        observation.pending = lifecycle_jobs(ordered_targets(attempt["structure"]))
                        observation.retry = ordered_targets(attempt["structure"])
                    elif (not active and (finishing.state in ("failed", "cancelled")
                                          or finishing.state == "pending" and finishing.intent is not None)
                          and all(jobs[local].state == "complete" for local in attempt["executions"])
                          and not _files.exists(store.record_path(attempt, "completion.json"))):
                        recover_transfer(store, observation, jobs)
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
                fresh_observation(store, observation, "no completed managed attempt")
            tasks.append(observation)
            selected[name] = observation
        consumers = admission.consumer_activity(store, backend, ctx.backend)
        for observation in tasks:
            if observation.attempt is not None:
                observation.consumers = consumers.get((observation.name, observation.attempt["attempt"]), {})
                if observation.action in ("fresh", "initialize", "transfer", "repair"):
                    activity = admission.replacement_activity(store, observation.attempt, backend, ctx.backend, consumers)
                    observation.consumers = activity.consumers
                    if activity.reason:
                        observation.action, observation.reason = "blocked", activity.reason
                        observation.pending = []
    return Plan(store, tasks)
