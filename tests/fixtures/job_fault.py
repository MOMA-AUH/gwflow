"""Test-only process/filesystem gates around durable record publication.

The backend invokes this wrapper as a real scheduled process. Abrupt exit
leaves the production atomic-publication implementation to handle the fault.
"""

import json
import os
from pathlib import Path
import runpy
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
    result = original_replace(source, destination, **kwargs)
    if destination == "manifest.json" and options.get("gate_after_manifest"):
        gate("manifest")
    return result


def rename(source, destination, **kwargs):
    if destination == "committed" and options.get("crash_before_commit"):
        os._exit(94)
    result = original_rename(source, destination, **kwargs)
    if destination == "committed" and options.get("crash_after_commit"):
        os._exit(95)
    return result


os.link, os.replace, os.rename = link, replace, rename
if options.get("gate_before_preparation"):
    gate("preparation")
# The backend passes the original interpreter's '-m gwflow.execution' arguments.
sys.argv = [sys.argv[3], *sys.argv[4:]]
runpy.run_module("gwflow.execution", run_name="__main__")
