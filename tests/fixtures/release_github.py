#!/usr/bin/env python3
"""Controlled GitHub release service for the artifact-command boundary tests."""

import json
import hashlib
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
if os.environ.get("GWFLOW_RELEASE_FAIL") == " ".join(args[:2]):
    sys.exit("injected GitHub failure")
if args[0] == "api":
    state = json.loads(state_path.read_text())
    print(json.dumps({"id": 1, "tag_name": state["tagName"], "draft": state["isDraft"],
                      "prerelease": state["isPrerelease"], "immutable": state.get("immutable", False),
                      "body": state.get("body", "Original release notes"),
                      "assets": [{"name": path.name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
                                 for path in (root / "assets").iterdir()]}))
elif args[:2] == ["release", "view"]:
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
    for filename in args[3:args.index("--repo")]:
        path = Path(filename)
        destination = root / "assets" / path.name
        if destination.exists() and ("--clobber" not in args or path.name != "SHA256SUMS"):
            sys.exit("asset already exists")
        shutil.copyfile(path, destination)
elif args[:2] == ["release", "download"]:
    name = args[args.index("--pattern") + 1]
    directory = Path(args[args.index("--dir") + 1])
    shutil.copyfile(root / "assets" / name, directory / name)
elif args[:2] == ["release", "edit"]:
    state = json.loads(state_path.read_text())
    state["body"] = Path(args[args.index("--notes-file") + 1]).read_text()
    state_path.write_text(json.dumps(state))
elif args[:2] == ["release", "delete-asset"]:
    if args[3].endswith(".conda"):
        sys.exit("fixture refuses package deletion")
    (root / "assets" / args[3]).unlink()
else:
    sys.exit("unsupported fixture request")
