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


work = Path(sys.argv[1])
options = json.loads((work / "job-fault.json").read_text())


def gate(name):
    (work / (name + "-held")).touch()
    deadline = time.monotonic() + 20
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
    if destination == "inputs.json" and options.get("gate_after_baseline"):
        gate("baseline")
    return result


def replace(source, destination, **kwargs):
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
    result = original_replace(source, destination, **kwargs)
    if destination == "installation.json" and options.get("crash_after_installation_intent"):
        os._exit(103)
    if destination == "manifest.json" and options.get("crash_after_manifest"):
        os._exit(98)
    if destination == "manifest.json" and options.get("gate_after_manifest"):
        gate("manifest")
    return result


def rename(source, destination, **kwargs):
    if destination == "committed" and options.get("crash_before_commit"):
        os._exit(94)
    result = original_rename(source, destination, **kwargs)
    if destination == "committed" and options.get("crash_after_commit"):
        os._exit(95)
    if destination != "committed" and options.get("crash_after_results_install"):
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
if options.get("gate_before_preparation"):
    gate("preparation")
# The backend passes the original interpreter's '-m gwflow.execution' arguments.
sys.argv = [sys.argv[3], *sys.argv[4:]]
runpy.run_module("gwflow.execution", run_name="__main__")
