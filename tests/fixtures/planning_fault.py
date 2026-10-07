"""Publish fixture evidence at real read/CLI planning boundaries."""

import json
import os
from pathlib import Path
import sys

from click.testing import CliRunner
from gwf.cli import main


scenario = json.loads(Path(sys.argv[1]).read_text())
actions = scenario["actions"]
results = []
invocation = 0


def fault(frame, event, arg):
    if event != "return":
        return
    module, name = frame.f_globals.get("__name__"), frame.f_code.co_name
    boundary = None
    if module == "gwflow.planning" and name == "plan_workflow":
        boundary = "planned"
    elif module == "gwflow.images" and name == "_readable" and arg is None:
        boundary = "image:" + str(frame.f_locals["path"])
    elif module == "gwflow._files" and name == "read_json" and arg is None:
        boundary = "missing:" + str(frame.f_locals["path"])
    elif module == "gwflow._files" and name == "metadata" and arg:
        boundary = "metadata:" + str(Path(frame.f_locals["root"]) / next(iter(arg)))
    elif module == "json" and name == "load":
        try:
            boundary = os.readlink(f"/proc/self/fd/{frame.f_locals['fp'].fileno()}")
        except (OSError, ValueError, AttributeError):
            return
    for action in list(actions):
        if action["pass"] != invocation or action["after"] != boundary:
            continue
        actions.remove(action)
        for path, value in action.get("write", {}).items():
            destination = Path(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(value))
        for path in action.get("remove", []):
            Path(path).unlink()
        for source, destination in action.get("rename", {}).items():
            Path(source).rename(destination)
        for path, target in action.get("symlink", {}).items():
            Path(path).symlink_to(target)
        if action.get("interrupt"):
            raise KeyboardInterrupt
        if action.get("error"):
            raise RuntimeError("Injected observation failure")


for invocation, arguments in enumerate(scenario["commands"]):
    sys.setprofile(fault)
    try:
        result = CliRunner().invoke(main, arguments)
        results.append({"exit_code": result.exit_code, "output": result.output})
    finally:
        sys.setprofile(None)
print(json.dumps({"results": results, "unreached_actions": actions}))
