"""Preserved schema-1 reader. Provisional until v1.0.0 publication.

This deliberately imports no gwflow code. Keep its original assertions when
adding later coverage; see docs/validation/compatibility/README.md.
"""

import json


def require(condition, message):
    if not condition:
        raise ValueError(message)


def field(record, name, *types):
    require(isinstance(record, dict) and name in record, f"Missing {name}")
    value = record[name]
    require(type(value) in types, f"Incompatible {name}")
    return value


def consume(text, *, details, exit_code):
    """Read exactly one report, retaining unfamiliar fields, values and prose."""
    document = json.loads(text)
    require(field(document, "schema_version", int) == 1, "Unsupported schema version")
    if "error" in document:
        require("tasks" not in document and "notices" not in document, "Partial error report")
        error = field(document, "error", dict)
        code = field(error, "code", str)
        field(error, "message", str)
        require(exit_code != 0, "Error reported as successful inspection")
        if code in ("usage_error", "inspection_failed"):
            require(exit_code == {"usage_error": 2, "inspection_failed": 1}[code], "Changed error exit")
    else:
        require(exit_code == 0, "Successful report has unsuccessful exit")
        for task in field(document, "tasks", list):
            for name in ("name", "state", "next_action"):
                field(task, name, str)
            field(task, "attempt_id", str, type(None))
            progress = field(task, "progress", dict)
            completed = field(progress, "completed", int, type(None))
            total = field(progress, "total", int)
            require(total >= 0 and (completed is None or 0 <= completed <= total), "Invalid progress")
            reason = field(task, "reason", dict)
            field(reason, "code", str)
            field(reason, "message", str)
            if details:
                for job in field(task, "jobs", list):
                    field(job, "name", str)
                    field(job, "state", str)
                    for name in ("submission_name", "backend_id", "observed_state"):
                        field(job, name, str, type(None))
                    field(job, "in_current_definition", bool)
            else:
                require("jobs" not in task, "Unrequested job details")
        for notice in field(document, "notices", list):
            field(notice, "code", str)
            field(notice, "message", str)
            require(all(type(name) is str for name in field(notice, "task_names", list)), "Invalid notice Tasks")
    return document


def job_outcome(job):
    """A script must not infer success or inactivity from a new state value."""
    return {"success": job["state"] == "completed",
            "inactive": job["state"] in ("completed", "failed", "canceled")}


def check_notices(document, expected):
    """Check established notices without discarding unfamiliar guidance."""
    known = {"workflow_blocked", "upstream_recovery_pending", "completion_followup_required",
             "submission_investigation_required"}
    observed = {notice["code"]: set(notice["task_names"]) for notice in document["notices"]
                if notice["code"] in known}
    require(observed == expected, "Changed established notices")


def check_handoff(document, *, case, attempt_id, identities):
    """Selected established meanings, independent of current serializer code.

    Only dynamic identifiers come from the baseline installation's observations.
    Names, counts, states, actions and reasons below are preserved expectations.
    """
    require(len(document["tasks"]) == 1, "Expected one selected Task")
    task = document["tasks"][0]
    if case == "cleaned":
        expected = ("cleaned", "reusable", "reuse", 3, 3, "results_valid")
        states = {"gwflow_prepare": "completed", "copy": "completed", "gwflow_complete": "completed"}
    elif case == "partial":
        expected = ("sample", "failed", "retry", 2, 5, "jobs_require_retry")
        states = {"gwflow_prepare": "completed", "left": "completed", "right": "failed",
                  "join": "failed", "gwflow_complete": "failed"}
    else:
        raise ValueError("Unknown handoff case")
    require((task["name"], task["state"], task["next_action"], task["progress"]["completed"],
             task["progress"]["total"], task["reason"]["code"]) == expected, "Changed handoff meaning")
    require(task["attempt_id"] == attempt_id and isinstance(attempt_id, str), "Changed attempt identity")
    check_notices(document, {})
    if "jobs" in task:
        jobs = {job["name"]: job for job in task["jobs"]}
        require(len(jobs) == len(task["jobs"]) == len(states), "Changed job count")
        require(set(jobs) == {expected[0] + "__" + name for name in states}, "Changed Public job names")
        for local, state in states.items():
            job = jobs[expected[0] + "__" + local]
            require(job["state"] == state, "Changed job state")
            require(job["observed_state"] == state, "Lost recorded job observation")
            require(job["in_current_definition"] is True, "Current job marked removed")
            require((job["submission_name"], job["backend_id"]) == identities[job["name"]], "Changed submission identity")
            require(isinstance(job["submission_name"], str) and isinstance(job["backend_id"], str),
                    "Lost recorded submission identity")
