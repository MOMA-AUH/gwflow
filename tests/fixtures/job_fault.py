"""Test-only process/filesystem gates around durable record publication.

The backend invokes this wrapper as a real scheduled process. Abrupt exit
leaves the production atomic-publication implementation to handle the fault.
"""

import json
import errno
import os
from pathlib import Path
import runpy
import shutil
import sys
import time
from types import SimpleNamespace


work = Path(sys.argv[1])
options = json.loads((work / "job-fault.json").read_text())
if "jobs" in options:
    # Select faults by the public job name, before running the real worker.
    task, operation = sys.argv[5], sys.argv[7]
    local = sys.argv[9] if operation == "execute" else "gwflow_prepare" if operation == "prepare" else "gwflow_complete"
    options = options["jobs"].get(f"{task}__{local}", {})
if options.get("crash_before_execution"):
    os._exit(109)


def gate(name):
    (work / (name + "-held")).touch()
    # A held job can be inspected by several installed CLI processes before release.
    deadline = time.monotonic() + 60
    while not (work / (name + "-release")).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("timed out waiting for filesystem fixture gate")
        time.sleep(0.025)


original_link, original_replace, original_rename = os.link, os.replace, os.rename
original_utime = os.utime
original_copyfileobj = shutil.copyfileobj
original_unlink, original_rmdir = os.unlink, os.rmdir


def link(source, destination, **kwargs):
    if destination == "inputs.json" and options.get("crash_before_baseline") and not (work / "crashed").exists():
        (work / "crashed").touch()
        os._exit(91)
    result = original_link(source, destination, **kwargs)
    if destination == "inputs.json" and options.get("crash_after_baseline") and not (work / "crashed").exists():
        (work / "crashed").touch()
        os._exit(92)
    return result


def replace(source, destination, **kwargs):
    phase = None
    if destination == "record-state.json" and options.get("compact_completion"):
        fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        with os.fdopen(fd) as stream:
            phase = json.load(stream)["phase"]
        if options.get("compaction_fault") == "before-" + phase:
            os._exit(110)
    if destination == "success.json" and options.get("crash_before_success"):
        os._exit(93)
    if destination == "manifest.json" and options.get("crash_before_manifest"):
        os._exit(97)
    if destination == "transfer.json" and options.get("crash_before_transfer_ownership"):
        os._exit(101)
    if destination == "completion.json" and options.get("crash_before_completion"):
        os._exit(99)
    if destination == "installation.json" and options.get("crash_before_installation_intent"):
        os._exit(102)
    if destination == "staging.json" and options.get("crash_before_staging_ownership"):
        os._exit(106)
    result = original_replace(source, destination, **kwargs)
    if phase and options.get("compaction_fault") == "after-" + phase:
        os._exit(111)
    if destination == "installation.json" and options.get("crash_after_installation_intent"):
        os._exit(103)
    if destination == "staging.json" and options.get("crash_after_staging_ownership"):
        os._exit(107)
    if destination == "manifest.json" and options.get("crash_after_manifest"):
        os._exit(98)
    return result


def rename(source, destination, **kwargs):
    if destination == "committed" and options.get("crash_before_commit"):
        os._exit(94)
    result = original_rename(source, destination, **kwargs)
    if str(source).startswith(".staging-") and options.get("crash_after_staging_directory_install"):
        os._exit(108)
    if destination == "committed" and options.get("crash_after_commit"):
        os._exit(95)
    if destination != "committed" and not str(source).startswith(".staging-") and options.get("crash_after_results_install"):
        os._exit(100)
    return result


def utime(path, *args, **kwargs):
    if isinstance(path, int) and options.get("unsupported_mtime"):
        raise OSError(errno.ENOTSUP, "fixture filesystem cannot preserve timestamps")
    if options.get("coarse_mtime") and "ns" in kwargs:
        kwargs["ns"] = tuple(value // 1_000_000_000 * 1_000_000_000 for value in kwargs["ns"])
    return original_utime(path, *args, **kwargs)


def copyfileobj(source, destination, *args, **kwargs):
    if options.get("crash_during_copy"):
        destination.write(source.read(1))
        destination.flush()
        os._exit(96)
    return original_copyfileobj(source, destination, *args, **kwargs)


def unlink(path, **kwargs):
    parent = Path(os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}")) if "dir_fd" in kwargs else None
    result = original_unlink(path, **kwargs)
    if (options.get("compaction_fault") == "during-deletion" and parent is not None
            and "attempts" in parent.parts and not str(path).startswith(".pending-")):
        os._exit(112)
    if options.get("crash_during_results_removal") and parent is not None and work / "results" in parent.parents:
        os._exit(104)
    return result


def rmdir(path, **kwargs):
    result = original_rmdir(path, **kwargs)
    if options.get("crash_after_results_removal") and path == "report":
        os._exit(105)
    return result


os.link, os.replace, os.rename = link, replace, rename
os.utime = utime
os.unlink, os.rmdir = unlink, rmdir
shutil.copyfileobj = copyfileobj
if options.get("device_offset"):
    device_offset = options["device_offset"] + os.getpid()
    def shifted_stat(function):
        def observe(*args, **kwargs):
            info = function(*args, **kwargs)
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_dev"] += device_offset
            return SimpleNamespace(**fields)
        return observe
    os.stat, os.fstat = shifted_stat(os.stat), shifted_stat(os.fstat)
if options.get("gate_before_preparation"):
    gate("preparation")
if "execution_path" in options:
    os.environ["PATH"] = options["execution_path"]


def published_record(frame, event, arg):
    # Pause after the logical publication releases its attempt lock. Crash
    # injection above still interrupts the actual filesystem operations.
    if (event == "return" and frame.f_globals.get("__name__") == "gwflow.lifecycle"
            and frame.f_code.co_name == "publish"):
        filename = frame.f_locals["filename"]
        if filename == "inputs.json" and options.get("gate_after_baseline"):
            gate("baseline")
        elif filename == "manifest.json" and options.get("gate_after_manifest"):
            gate("manifest")


if options.get("gate_after_baseline") or options.get("gate_after_manifest"):
    sys.setprofile(published_record)
if options.get("compact_completion"):
    # Test-only activation at the existing completion-outcome boundary. Runtime
    # activation remains a separate ticket; production uses the same primitive.
    from gwflow import admission
    from gwflow.lifecycle import Store
    original_publish = Store.publish

    def publish_with_compaction(store, attempt, filename, kind, **fields):
        if kind == "job-outcome" and fields.get("state") == "complete":
            intent = admission.read_intent(store, attempt, "gwflow_complete")
            store.compact(attempt, intent)
            return
        return original_publish(store, attempt, filename, kind, **fields)

    Store.publish = publish_with_compaction
# The backend passes the original interpreter's '-m gwflow.execution' arguments.
sys.argv = [sys.argv[3], *sys.argv[4:]]
runpy.run_module("gwflow.execution", run_name="__main__")
