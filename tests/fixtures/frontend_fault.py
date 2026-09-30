"""Interrupt a real CLI at initialization filesystem boundaries."""

import os
import json
from pathlib import Path
import sys
import time

from gwf.cli import main


work, phase, task = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.argv = ["gwf", *sys.argv[4:]]
original_replace, original_unlink, original_rename = os.replace, os.unlink, os.rename


def directory(kwargs, key):
    return Path(os.readlink(f"/proc/self/fd/{kwargs[key]}")) if key in kwargs else None


def replace(source, destination, **kwargs):
    parent = directory(kwargs, "dst_dir_fd")
    selected = parent is not None and f"/tasks/{task}/" in str(parent) + "/"
    if selected and destination == "initialization.json" and phase == "before_initialization_intent":
        os._exit(91)
    if destination == "owner.json" and phase == "before_work_recreation":
        os._exit(95)
    result = original_replace(source, destination, **kwargs)
    if (destination == "owner.json" and phase == "before_work_install"
            and json.loads((parent / destination).read_text()).get("work_recreation")):
        os._exit(96)
    if selected and destination == "current.json" and phase == "before_removal":
        os._exit(92)
    if selected and destination == "current.json" and phase == "gate_before_removal":
        (work / "frontend-held").touch()
        deadline = time.monotonic() + 20
        while not (work / "frontend-release").exists():
            if time.monotonic() > deadline:
                os._exit(98)
            time.sleep(0.025)
    if selected and destination == "ready.json" and phase == "after_ready":
        os._exit(93)
    return result


def rename(source, destination, **kwargs):
    result = original_rename(source, destination, **kwargs)
    if phase == "after_work_install" and directory(kwargs, "dst_dir_fd") == work and destination == "work":
        os._exit(97)
    return result


def unlink(path, **kwargs):
    parent = directory(kwargs, "dir_fd")
    result = original_unlink(path, **kwargs)
    if phase == "during_removal" and parent == work / "results" / task:
        os._exit(94)
    return result


os.replace, os.unlink, os.rename = replace, unlink, rename
main()
