#!/usr/bin/env python3
"""Controlled external interpreter observation; cannot execute upgrade journeys."""

import json
from pathlib import Path
import sys


if sys.argv[1:3] != ["-I", "-c"]:
    sys.exit("This fixture supports only installed-environment inspection")
prefix = Path(__file__).resolve().parents[1]
packages = [json.loads(path.read_text()) for path in (prefix / "conda-meta").glob("*.json")]
site = prefix / "lib"
identity = {"device": site.stat().st_dev, "inode": site.stat().st_ino}
print(json.dumps({
    "executable": str(Path(__file__).resolve()), "prefix": str(prefix), "base_prefix": str(prefix),
    "prefix_identity": {"device": prefix.stat().st_dev, "inode": prefix.stat().st_ino},
    "package": str(site / "gwflow"), "stdlib": str(site), "sys_path": [str(site)],
    "python_version": "3.12.14", "imports": {}, "markers": {},
    "distributions": [{"name": item["name"], "version": item["version"], "location": str(site),
                       "requires_python": ">=3.12,<3.13", "requires": []} for item in packages],
    "runtime_directories": [{"path": str(site), **identity}],
}))
