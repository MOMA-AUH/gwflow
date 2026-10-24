"""Interrupt a real CLI at initialization filesystem boundaries."""

import os
import json
from pathlib import Path
import sys
import time

from gwflow.cli import main


work, phase, task = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.argv = ["gwflow", *sys.argv[4:]]
original_replace, original_unlink, original_rename, original_rmdir = os.replace, os.unlink, os.rename, os.rmdir
original_symlink, original_link = os.symlink, os.link
recovery_writes = 0


def directory(kwargs, key):
    return Path(os.readlink(f"/proc/self/fd/{kwargs[key]}")) if key in kwargs else None


def gate(name):
    (work / (name + "-held")).touch()
    deadline = time.monotonic() + 20
    while not (work / (name + "-release")).exists():
        if time.monotonic() > deadline:
            os._exit(98)
        time.sleep(0.025)


def replace(source, destination, **kwargs):
    global recovery_writes
    parent = directory(kwargs, "dst_dir_fd")
    selected = parent is not None and f"/tasks/{task}/" in str(parent) + "/"
    if selected and destination == "recovery.json":
        recovery_writes += 1
        if phase == "recovery_second_write_error" and recovery_writes == 2:
            raise OSError("injected recovery publication failure")
    if selected and destination == "initialization.json" and phase == "before_initialization_intent":
        os._exit(91)
    if selected and destination == "current.json" and phase == "before_selection":
        os._exit(90)
    if selected and destination == "repair.json" and phase == "before_repair_intent":
        os._exit(99)
    if selected and destination == "ack.json" and phase == "ack_write_error":
        raise OSError("injected acknowledgement storage failure")
    if selected and destination == "recovery.json" and phase == "before_recovery_record":
        os._exit(112)
    if selected and destination == "attempt.json" and phase == "before_repair_selection":
        os._exit(100)
    if destination == "owner.json" and phase == "before_work_recreation":
        os._exit(95)
    if selected and destination == "cleanup.json" and phase == "cleanup_before_completion":
        if json.loads((parent / source).read_text()).get("state") == "removed":
            os._exit(105)
    if selected and destination == "record-state.json" and phase in (
            "cleanup_before_completion", "cleanup_before_remove", "gate_cleanup"):
        cleanup = json.loads((parent / source).read_text()).get("records", {}).get("cleanup.json")
        if cleanup and phase == "cleanup_before_completion" and cleanup["state"] == "removed":
            os._exit(105)
    compacted_cleanup = None
    if selected and destination == "record-state.json" and phase.startswith("compacted_cleanup_"):
        compacted_cleanup = json.loads((parent / source).read_text()).get("records", {}).get("cleanup.json")
        if compacted_cleanup:
            boundary = "intent" if compacted_cleanup["state"] == "removing" else "outcome"
            if phase == "compacted_cleanup_error_before_" + boundary:
                raise OSError("injected compacted cleanup publication failure")
            if phase == "compacted_cleanup_before_" + boundary:
                os._exit(114)
    restoration_phase = None
    if selected and destination == "record-state.json" and phase.startswith("restoration_"):
        restoration_phase = json.loads((parent / source).read_text())["phase"]
        if phase == "restoration_before_" + restoration_phase:
            os._exit(116)
    result = original_replace(source, destination, **kwargs)
    if restoration_phase and phase == "restoration_after_" + restoration_phase:
        os._exit(117)
    if restoration_phase and phase == "restoration_error_after_" + restoration_phase:
        raise OSError("injected restoration synchronization uncertainty")
    if compacted_cleanup and phase == "compacted_cleanup_error_after_" + boundary:
        raise OSError("injected compacted cleanup replacement synchronization uncertainty")
    if compacted_cleanup and phase == "compacted_cleanup_after_" + boundary:
        os._exit(115)
    if selected and destination == "recovery.json" and phase == "recovery_second_write_interrupt" and recovery_writes == 2:
        raise KeyboardInterrupt
    if selected and destination == "recovery.json" and phase == "after_recovery_record":
        os._exit(113)
    if selected and destination == "ack.json" and phase == "after_preparation_ack":
        if "__gwflow_prepare__" in json.loads((parent / destination).read_text())["job"]:
            os._exit(110)
    if selected and destination == "ack.json" and phase == "after_computation_ack":
        job = json.loads((parent / destination).read_text())["job"]
        if "__gwflow_prepare__" not in job and "__gwflow_complete__" not in job:
            os._exit(109)
    if selected and destination == "ack.json" and phase.startswith("after_job_ack:"):
        job = json.loads((parent / destination).read_text())["job"]
        if f"__{phase.split(':', 1)[1]}__" in job:
            os._exit(111)
    cleanup_written = selected and (destination == "cleanup.json" or (
        destination == "record-state.json" and phase in ("cleanup_before_remove", "gate_cleanup")
        and json.loads((parent / destination).read_text()).get("records", {}).get("cleanup.json")))
    if cleanup_written and phase == "cleanup_before_remove":
        os._exit(102)
    if cleanup_written and phase == "gate_cleanup":
        gate("cleanup")
    if selected and destination == "attempt.json" and phase == "after_repair_selection":
        os._exit(101)
    if (destination == "owner.json" and phase == "before_work_install"
            and json.loads((parent / destination).read_text()).get("work_recreation")):
        os._exit(96)
    if selected and destination == "current.json" and phase == "before_removal":
        os._exit(92)
    if selected and destination == "current.json" and phase == "gate_before_removal":
        gate("frontend")
    if selected and destination == "ready.json" and phase == "after_ready":
        os._exit(93)
    return result


def rename(source, destination, **kwargs):
    result = original_rename(source, destination, **kwargs)
    if phase == "restoration_during_install" and any(
            part.startswith(".restoring-") for part in directory(kwargs, "src_dir_fd").parts):
        os._exit(118)
    if phase == "after_work_install" and directory(kwargs, "dst_dir_fd") == work and destination == "work":
        os._exit(97)
    return result


def unlink(path, **kwargs):
    parent = directory(kwargs, "dir_fd")
    result = original_unlink(path, **kwargs)
    if phase == "during_removal" and parent == work / "results" / task:
        os._exit(94)
    if phase == "cleanup_during_remove" and parent is not None and work / "work" / task in parent.parents:
        os._exit(103)
    if phase == "cleanup_remove_error" and parent is not None and work / "work" / task in parent.parents:
        raise OSError("injected cleanup removal failure")
    if phase == "cleanup_remove_interrupt" and parent is not None and work / "work" / task in parent.parents:
        raise KeyboardInterrupt
    return result


def rmdir(path, **kwargs):
    parent = directory(kwargs, "dir_fd")
    if phase == "cleanup_staging_error" and parent is not None and "/staging/" in str(parent) + "/":
        raise OSError("injected staging removal failure")
    result = original_rmdir(path, **kwargs)
    if phase == "cleanup_after_workspace_remove" and parent == work / "work" / task:
        os._exit(104)
    return result


def symlink(source, destination, **kwargs):
    if phase == "submission_log_error" and destination == task + "__gwflow_prepare.stdout":
        raise OSError("injected submission log alias failure")
    return original_symlink(source, destination, **kwargs)


def link(source, destination, **kwargs):
    result = original_link(source, destination, **kwargs)
    parent = directory(kwargs, "dst_dir_fd")
    if phase == "restoration_during_reconstruction" and parent is not None and any(
            part.startswith(".restoring-") for part in parent.parts):
        os._exit(119)
    return result


os.replace, os.unlink, os.rename, os.rmdir = replace, unlink, rename, rmdir
os.link = link
os.symlink = symlink
main()
