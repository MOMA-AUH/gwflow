#!/usr/bin/env python3
"""Controlled Conda/channel command boundary; never contacts a package service."""

import json
import os
from pathlib import Path
import shutil
import sys

root = Path(os.environ["GWFLOW_CHANNEL_FIXTURE_ROOT"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:4] == ["run", "-n", "base", "anaconda"]:
    if "--force" in args or "--skip-existing" in args:
        sys.exit("fixture refuses unchecked replacement or acceptance")
    destination = root / "package.conda"
    if destination.exists():
        sys.exit("distribution already exists")
    shutil.copyfile(args[-1], destination)
elif args[0] == "create":
    if os.environ.get("GWFLOW_PUBLICATION_FAIL") == "install":
        sys.exit("injected channel installation failure")
    Path(args[args.index("-p") + 1]).mkdir(parents=True, exist_ok=True)
elif "tests/release_smoke.py" in args:
    if os.environ.get("GWFLOW_PUBLICATION_FAIL") == "smoke":
        sys.exit("injected smoke failure after upload")
elif "tests/release_provenance.py" in args:
    if os.environ.get("GWFLOW_PUBLICATION_FAIL") == "provenance":
        sys.exit("injected provenance failure")
    bundle = Path(args[args.index("--artifacts") + 1])
    release = json.loads((bundle / "release.json").read_text())
    provenance = {"release": release["release"], "source_commit": release["source_commit"],
                  "workflow_run": os.environ["GITHUB_RUN_ID"],
                  "artifact": {**release["artifact"], "url": "https://conda.anaconda.org/MOMA-AUH/noarch/" + release["artifact"]["filename"]}}
    Path(args[args.index("--output") + 1]).write_text(json.dumps(provenance))
elif "pip" not in args:
    sys.exit("unsupported fixture request")
