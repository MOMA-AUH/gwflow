#!/usr/bin/env python3
"""Controlled Conda/channel command boundary; never contacts a package service."""

import json
import os
from pathlib import Path
import shutil
import subprocess
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
    # Supply only the installed-package boundary. Execute the actual provenance
    # entry point so its identity and checksum checks cannot be hidden by a stub.
    prefix = Path(args[args.index("-p") + 1])
    site = prefix / "site-packages"
    (site / "gwflow").mkdir(parents=True)
    (site / "gwflow/__init__.py").write_text("")
    version = release["release"][1:]
    metadata = site / f"gwflow-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: gwflow\nVersion: {version}\n")
    records = prefix / "conda-meta"
    records.mkdir()
    installed = {"fn": release["artifact"]["filename"], "sha256": release["artifact"]["sha256"],
                 "build": "py_0", "url": "https://conda.anaconda.org/MOMA-AUH/noarch/" + release["artifact"]["filename"]}
    if os.environ.get("GWFLOW_PUBLICATION_FAIL") == "installed-checksum":
        installed["sha256"] = "0" * 64
    (records / f"gwflow-{version}-py_0.json").write_text(json.dumps(installed))
    command = ("import runpy, sys; from pathlib import Path; "
               "sys.prefix = sys.argv.pop(1); sys.path.insert(0, sys.argv.pop(1)); "
               "sys.argv = sys.argv[1:]; sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent)); "
               "runpy.run_path(sys.argv[0], run_name='__main__')")
    result = subprocess.run([sys.executable, "-c", command, str(prefix), str(site),
                             *args[args.index("tests/release_provenance.py"):]])
    sys.exit(result.returncode)
elif "pip" not in args:
    sys.exit("unsupported fixture request")
