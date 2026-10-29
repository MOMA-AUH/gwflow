#!/usr/bin/env python3
"""Controlled GitHub release service for the artifact-command boundary tests."""

import json
import os
from pathlib import Path
import shutil
import sys


root = Path(os.environ["GWFLOW_RELEASE_FIXTURE_ROOT"])
root.mkdir(exist_ok=True)
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
state_path = root / "state.json"
if args[:2] == ["release", "view"]:
    if not state_path.exists():
        sys.exit("release not found")
    state = json.loads(state_path.read_text())
    state["assets"] = [{"name": path.name} for path in (root / "assets").iterdir()]
    print(json.dumps(state))
elif args[:2] == ["release", "create"]:
    if "--draft" not in args or "--verify-tag" not in args:
        sys.exit("fixture refuses publication or tag creation")
    state_path.write_text(json.dumps({"tagName": args[2], "isDraft": True, "isPrerelease": False}))
    (root / "assets").mkdir()
elif args[:2] == ["release", "upload"]:
    if "--clobber" in args:
        sys.exit("fixture refuses asset replacement")
    for filename in args[3:args.index("--repo")]:
        path = Path(filename)
        destination = root / "assets" / path.name
        if destination.exists():
            sys.exit("asset already exists")
        shutil.copyfile(path, destination)
elif args[:2] == ["release", "download"]:
    name = args[args.index("--pattern") + 1]
    directory = Path(args[args.index("--dir") + 1])
    shutil.copyfile(root / "assets" / name, directory / name)
else:
    sys.exit("unsupported fixture request")
