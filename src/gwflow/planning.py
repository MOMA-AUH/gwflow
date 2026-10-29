"""Shared observations and lifecycle decisions, with no attempt allocation."""

from contextlib import ExitStack
from dataclasses import dataclass

from gwf.backends import BackendStatus, create_backend
from gwf.exceptions import WorkflowError

from . import _files, _observations, _records, admission, images, inputs, transfer
from .activity import activity as report_activity
from .inspection_errors import InspectionBlocker
from .lifecycle import Store, TaskObservation, declarations, ordered_tasks, ordered_targets, producer_names, target_dependencies
from .selection import task_selection
from .workflow import lifecycle_jobs


@dataclass
class Plan:
    store: Store
    tasks: list[TaskObservation]

    @property
    def blocked(self):
        return [task for task in self.tasks if task.action == "blocked"]


@_records.observing
def tracked_commands_match(store, attempt, commands):
    if not attempt["command_tracking"] or commands != attempt["commands"]:
        return False
    for local, command in commands.items():
        try:
            execution = store.execution(attempt, local)
        except WorkflowError:
            return False
        if not execution["command_tracking"] or execution["command"] != command:
            return False
    return True


def definition_change(store, observation, observations_by_name, *, force_option, initializing):
    attempt = observation.attempt
    if force_option:
        return "definition_requires_refresh", f"requested {force_option}"
    if observation.structure != attempt["structure"]:
        labels = {"inputs": "external inputs", "targets": "target membership or file declarations", "retained": "retained mappings"}
        changed = [label for key, label in labels.items() if observation.structure[key] != attempt["structure"][key]]
        return "definition_requires_refresh", "changed declared structure: " + ", ".join(changed)
    for producer in producer_names(observation.structure):
        upstream = observations_by_name[producer]
        expected = attempt["producers"].get(producer)
        if upstream.action == "fresh":
            return "upstream_attempt_changed", f"producer {producer} will start a fresh attempt; expected producer attempt was {expected}"
        if upstream.attempt["attempt"] != expected:
            return "upstream_attempt_changed", f"producer {producer} attempt changed: expected {expected}, current {upstream.attempt['attempt']}"
    changed = [local for local, command in observation.commands.items() if command != attempt["commands"][local]]
    if changed:
        return "definition_requires_refresh", "changed commands: " + ", ".join(changed)
    if not attempt["command_tracking"]:
        return "definition_requires_refresh", "command tracking required; previous commands were not tracked"
    if not initializing and not tracked_commands_match(store, attempt, observation.commands):
        return "definition_requires_refresh", "selected execution commands differ or lack a tracked baseline"
    return None


@_records.observing
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
    observation.repeated_jobs.update([*replaced, "gwflow_complete"])
    store.check_inputs(attempt)
    if not observation.work_present:
        raise WorkflowError("Missing attempt workspace requires a fresh attempt")
    if any(store.exists(attempt, filename) for filename in ("manifest.json", "installed.json", "completion.json")):
        raise WorkflowError("Existing results finishing evidence requires recovery before computation retry")
    admission.require_retry_inactive(jobs, replaced)
    observation.retry = replaced
    observation.pending = [local for local in ordered_targets(attempt["structure"])
                           if local in replaced or jobs[local].state == "pending"]
    observation.action, observation.reason = "retry", "retry target executions: " + ", ".join(replaced)
    observation.reason_code = "jobs_require_retry"
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


def fresh_observation(store, observation, reason, *, code="prior_work_unusable", details=None):
    observation.restart_required = True
    active = [item.submission for item in observation.submissions.values() if item.state in ("active", "uncertain")]
    if active:
        blocker = ("submission_unresolved" if any(item.state == "uncertain" for item in observation.submissions.values())
                   else "activity_conflict")
        raise InspectionBlocker(blocker, "Unresolved or active work blocks replacement: " + ", ".join(active))
    inputs.observe([value for value in observation.structure["inputs"] if isinstance(value, str)], store.locations)
    observation.removal = store.result_removal(observation.attempt) if observation.attempt else None
    observation.action, observation.reason = "fresh", reason + "; fresh attempt required"
    observation.reason_code = code
    observation.reason_details = [reason] if details is None else details
    if observation.removal is not None:
        observation.reason += f"; remove previous results at {store.result_dir(observation.attempt)} before submission"
    observation.pending = lifecycle_jobs(ordered_targets(observation.structure))


def input_metadata_changes(store, observation):
    attempt = observation.attempt
    if not store.exists(attempt, "inputs.json"):
        return []
    baseline = store.input_baseline(attempt)
    try:
        observed = store.observe_inputs(attempt)
    except WorkflowError as error:
        raise InspectionBlocker("input_unavailable", f"Inputs unavailable; a fresh attempt requires valid inputs: {error}") from error
    changes = []
    for path, metadata in observed.items():
        previous = baseline["inputs"][path]
        fields = [f"{key}: {previous[key]!r} -> {value!r}" for key, value in metadata.items() if previous[key] != value]
        if fields:
            changes.append(f"{path} ({', '.join(fields)})")
            observation.input_changes[path] = {"previous": previous, "current": metadata}
    return changes


def recover_transfer(store, observation, jobs):
    try:
        recovery = transfer.inspect(store, observation.attempt)
    except transfer.InvalidSources:
        attempt = observation.attempt
        if (observation.work_present and store.repair_intent(attempt) is None
                and not any(store.exists(attempt, filename)
                            for filename in ("manifest.json", "installed.json", "completion.json"))
                and retry_observation(store, observation, jobs)):
            return
        fresh_observation(store, observation, "transfer sources cannot be recovered")
        return
    observation.action, observation.reason = "transfer", recovery.reason
    observation.reason_code = "completion_pending"
    observation.removal = recovery.removal
    observation.repeated_jobs.add("gwflow_complete")
    observation.pending = ["gwflow_complete"]


def completed_observation(store, observation):
    try:
        completed = store.completed(observation.attempt)
    except (WorkflowError, OSError):
        completed = False
    if completed:
        finishing = observation.submissions["gwflow_complete"]
        if (not store.compaction_succeeded(observation.attempt)
                or finishing.state in ("failed", "cancelled")):
            observation.action = "compact"
            observation.reason = "record compaction unfinished; retry completion only with gwflow run"
            observation.reason_code = "completion_pending"
            observation.repeated_jobs.add("gwflow_complete")
            observation.pending = ["gwflow_complete"]
            return
        observation.action, observation.reason = "reuse", "checked completion and retained metadata match"
        observation.reason_code = "results_valid"
        return
    retained_changes = store.retained_changes(observation.attempt)
    if store.cleanup_record(observation.attempt) is not None:
        # A discarded baseline can explain a change when still readable, but
        # cannot be required to start fresh after explicit cleanup.
        try:
            changes = input_metadata_changes(store, observation)
        except (WorkflowError, OSError):
            changes = []
        reason = ("input metadata changed: " + "; ".join(changes) if changes else
                  "work marked for cleanup")
        fresh_observation(store, observation, "; ".join([reason, *retained_changes]),
                          code="inputs_changed" if changes else "prior_work_unusable",
                          details=retained_changes if changes else None)
        return
    try:
        repair = transfer.inspect_repair(store, observation.attempt)
    except transfer.InvalidSources:
        fresh_observation(store, observation, "; ".join(["retained repair sources are unavailable or invalid", *retained_changes]))
        return
    observation.action, observation.reason = "repair", "restore damaged retained results from checked work under the same attempt"
    observation.reason_code = "results_need_repair"
    observation.removal = repair.removal
    observation.repeated_jobs.add("gwflow_complete")
    if retained_changes:
        observation.reason += "; " + "; ".join(retained_changes)
    observation.pending = ["gwflow_complete"]


def awaiting_producer_metadata(store, observation):
    if observation.action in ("blocked", "deferred", "repair", "transfer", "compact"):
        return True
    attempt = observation.attempt
    return (observation.action == "active" and store.repair_intent(attempt) is not None
            and store.read(attempt, "completion.json", "completion", operation=attempt["operation"]) is None)


@_observations.planning_pass()
@report_activity("Preparing Task inspection...")
def plan_workflow(workflow, ctx, *, selectors=(), force=False):
    selected_names = task_selection(workflow._task_declarations, selectors)
    expected = {f"{name}__{local}" for name, task in workflow._task_declarations.items()
                for local in lifecycle_jobs(task.targets)}
    if set(workflow.targets) != expected:
        raise WorkflowError("Every computation target in gwflow.Workflow must belong to a registered Task")
    store = Store.for_workflow(workflow)
    declared = {name: declarations(task, store, workflow) for name, task in workflow._task_declarations.items()}
    order = ordered_tasks(declared)
    scope = set(selected_names)
    # Validate the entire graph first, then include upstream dependencies by
    # walking backward through its dependency order.
    for name in reversed(order):
        if name in scope:
            scope.update(producer_names(declared[name][0]))
    tasks = []
    observations_by_name = {}
    with ExitStack() as resources:
        with report_activity("Waiting for scheduler..."):
            backend = resources.enter_context(create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config))
        with report_activity("Inspecting Tasks...", total=len(scope)) as inspected:
            for name in order:
                if name not in scope:
                    continue
                structure, commands = declared[name]
                attempt = store.current(name, workflow._result_dirs[name])
                observation = TaskObservation(name, "fresh", "no completed managed attempt", structure, commands, attempt)
                try:
                    images.resolve(workflow._task_declarations[name], structure, store.locations)
                except WorkflowError as error:
                    observation.action, observation.reason = "blocked", str(error)
                    observation.reason_code = "image_unavailable"
                    tasks.append(observation)
                    observations_by_name[name] = observation
                    inspected(len(tasks))
                    continue
                if attempt is not None:
                    try:
                        initializing = store.read(attempt, "ready.json", "ready") is None
                        if initializing:
                            store.initialization(attempt)
                            if store.exists(attempt, "admissions"):
                                raise WorkflowError("Task initialization is not ready but has submission evidence")
                        observation.work_present = _files.exists(store.workspace(attempt))
                        jobs = admission.observe(store, attempt, backend, ctx.backend)
                        finishing = jobs["gwflow_complete"]
                        observation.submissions = jobs
                        observation.observations_available = True
                        superseded = admission.superseded_activity(store, attempt, backend, ctx.backend)
                        observation.superseded_submissions = superseded
                        uncertain = [item.submission for item in jobs.values() if item.state == "uncertain"]
                        active = [item.submission for item in jobs.values() if item.state == "active"]
                        pending = pending_submissions(attempt, jobs)
                        failed = [local for local, item in jobs.items() if item.state in ("failed", "cancelled") and local not in pending]
                        if superseded:
                            observation.action, observation.reason = "blocked", "Unresolved or active recovered submissions: " + ", ".join(item.submission for item in superseded)
                            observation.reason_code = ("submission_unresolved" if any(item.state == "uncertain" for item in superseded)
                                                       else "activity_conflict")
                        elif uncertain:
                            observation.action, observation.reason = "blocked", "unresolved submission: " + ", ".join(uncertain)
                            observation.reason_code = "submission_unresolved"
                        elif change := definition_change(store, observation, observations_by_name,
                                                          force_option="--force" if force and name in selected_names else None,
                                                          initializing=initializing):
                            code, reason = change
                            fresh_observation(store, observation, reason, code=code)
                        elif initializing:
                            observation.restart_required = True
                            observation.removal = store.result_removal(attempt)
                            observation.action, observation.reason = "initialize", "resume selected initialization; remove previous results before submission"
                            observation.reason_code = "initialization_incomplete"
                            observation.pending = lifecycle_jobs(ordered_targets(structure))
                        elif not active and any(awaiting_producer_metadata(store, observations_by_name[producer])
                                                for producer in producer_names(structure)) and store.exists(attempt, "inputs.json"):
                            observation.action, observation.reason = "deferred", "producer results need recovery; a later invocation must replan input validity after restored metadata is available"
                            observation.reason_code = "upstream_recovery_pending"
                        elif not active and store.cleanup_record(attempt) is not None:
                            completed_observation(store, observation)
                        elif changes := input_metadata_changes(store, observation):
                            fresh_observation(store, observation, "input metadata changed: " + "; ".join(changes),
                                              code="inputs_changed", details=[])
                        elif not active and store.read(attempt, "completion.json", "completion", operation=attempt["operation"]) is not None:
                            completed_observation(store, observation)
                        elif not active and store.repair_intent(attempt) is not None:
                            recover_transfer(store, observation, jobs)
                        elif ((jobs["gwflow_prepare"].state in ("failed", "cancelled")
                               or jobs["gwflow_prepare"].backend_state in (BackendStatus.FAILED, BackendStatus.CANCELLED))
                              and not active and observation.work_present
                              and not any(_files.exists(store.execution_dir(attempt, local)) for local in attempt["executions"])):
                            if store.exists(attempt, "inputs.json"):
                                store.check_inputs(attempt)
                            observation.action, observation.reason = "prepare", "restart interrupted preparation in the same attempt"
                            observation.reason_code = "jobs_require_retry"
                            observation.repeated_jobs.update(lifecycle_jobs(structure["targets"]))
                            observation.pending = lifecycle_jobs(ordered_targets(attempt["structure"]))
                            observation.retry = ordered_targets(attempt["structure"])
                        elif (not active and (finishing.state in ("failed", "cancelled")
                                              or finishing.state == "pending" and finishing.intent is not None)
                              and all(jobs[local].state == "complete" for local in attempt["executions"])
                              and not store.exists(attempt, "completion.json")):
                            recover_transfer(store, observation, jobs)
                        elif retry_observation(store, observation, jobs):
                            pass
                        elif not failed and pending:
                            if store.exists(attempt, "inputs.json"):
                                store.check_inputs(attempt)
                            observation.action, observation.reason = "continue", "continue known submissions without repeating admitted work"
                            observation.reason_code = "submissions_pending"
                            observation.pending = pending
                        elif active:
                            observation.action, observation.reason = "active", "queued/running work: " + ", ".join(active)
                            observation.reason_code = "jobs_active"
                        elif store.execution_ready(attempt):
                            observation.action, observation.reason = "reuse", "checked completion and retained metadata match"
                            observation.reason_code = "results_valid"
                        else:
                            observation.action, observation.reason = "blocked", "incomplete attempt or damaged results; retry/recovery is not yet supported"
                            observation.reason_code = "validation_failed"
                    except (WorkflowError, OSError) as error:
                        observation.action, observation.reason = "blocked", str(error)
                        observation.reason_code = error.code if isinstance(error, InspectionBlocker) else "validation_failed"
                else:
                    fresh_observation(store, observation, "no completed managed attempt", code="no_attempt")
                tasks.append(observation)
                observations_by_name[name] = observation
                inspected(len(tasks))
        with report_activity("Checking Task dependencies..."):
            changing_results = {task.name for task in tasks if task.attempt is not None
                                and task.action in ("fresh", "initialize", "transfer", "repair")}
            consumers = admission.consumer_activity(store, backend, ctx.backend, producers=changing_results)
            for observation in tasks:
                if observation.attempt is not None:
                    observation.consumers = consumers.get((observation.name, observation.attempt["attempt"]), {})
                    if observation.action in ("fresh", "initialize", "transfer", "repair"):
                        activity = admission.replacement_activity(store, observation.attempt, backend, ctx.backend, consumers)
                        observation.consumers = activity.consumers
                        if activity.reason:
                            observation.action, observation.reason = "blocked", activity.reason
                            observation.reason_code = activity.reason_code
                            observation.pending = []
    return Plan(store, tasks)
