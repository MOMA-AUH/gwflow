#!/usr/bin/env python3
"""Controlled Conda process for runtime preservation acceptance tests."""

import json
import hashlib
import os
from pathlib import Path
import sys


arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("conda 26.9.4 (controlled fixture)")
    sys.exit(0)
prefix = Path(arguments[arguments.index("--prefix") + 1])
if arguments[0] == "create":
    explicit = Path(arguments[arguments.index("--file") + 1]).read_text().splitlines()
    if explicit[0] != "@EXPLICIT" or "--no-default-packages" not in arguments:
        sys.exit("Fixture requires explicit installation without a solve or default packages")
    if os.environ.get("GWFLOW_CONDA_UNAVAILABLE"):
        sys.exit("Fixture dependency artifact unavailable (controlled 404)")
    (prefix / "conda-meta").mkdir(parents=True)
    for entry in explicit[1:]:
        url, checksum = entry.rsplit("#", 1)
        filename = url.rsplit("/", 1)[1]
        name, version, build = filename.removesuffix(".conda").rsplit("-", 2)
        subdir = "noarch" if name == "gwflow" else ("linux-64" if url.startswith("file:") else url.rsplit("/", 2)[-2])
        record = {"name": name, "version": version, "build": build, "subdir": subdir,
                  "url": url, "fn": filename, "sha256": checksum}
        relative = f"lib/{name}.payload"
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name + " installed payload")
        installed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        record.update(files=[relative], paths_data={"paths": [
            {"_path": relative, "path_type": "hardlink", "sha256": installed_hash, "sha256_in_prefix": installed_hash}]})
        if name == os.environ.get("GWFLOW_CONDA_SUBSTITUTE"):
            record["version"] = "99.0"
        (prefix / "conda-meta" / (name + ".json")).write_text(json.dumps(record))
    (prefix / "installation.json").write_text(json.dumps({"explicit": explicit, "cache": os.environ.get("CONDA_PKGS_DIRS")}))
    sys.exit(0)
records = [json.loads(path.read_text()) for path in sorted((prefix / "conda-meta").glob("*.json"))]
if "--explicit" in arguments:
    print("# platform: linux-64\n@EXPLICIT")
    for record in records:
        if (prefix / "omit-export").exists() and record["name"] == "libnative":
            continue
        print(record["url"] + "#" + record["sha256"])
elif "--json" in arguments:
    extra = prefix / "extra-packages.json"
    print(json.dumps([{"name": record["name"], "version": record["version"],
                       "build_string": record["build"], "channel": record["url"],
                       "platform": record["subdir"]} for record in records]
                     + (json.loads(extra.read_text()) if extra.exists() else [])))
else:
    sys.exit("Unsupported fixture command")
