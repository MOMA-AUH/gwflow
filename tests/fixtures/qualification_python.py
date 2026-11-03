#!/usr/bin/env python3
"""Control pip and smoke commands while qualification checks real source files."""

import json
import os
from pathlib import Path
import sys
import tomllib


prefix = Path(__file__).resolve().parents[1]
arguments = sys.argv[1:]
if arguments[:2] == ["-I", "-c"]:
    sys.exit(0)
if arguments[:3] == ["-m", "pip", "wheel"]:
    destination = Path(arguments[arguments.index("--wheel-dir") + 1])
    projects = []
    for value in arguments[-2:]:
        package = Path(value)
        assert package.parent.as_posix().endswith(os.environ["GWFLOW_QUALIFICATION_LAYOUT"]), package
        assert (package / "src").is_dir(), package
        project = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        projects.append({"name": project["name"], "version": project["version"], "channel": "pypi"})
        (destination / (project["name"].replace("-", "_") + ".whl")).write_text(json.dumps(projects[-1]))
    (prefix / "qualified-projects.json").write_text(json.dumps(projects))
elif arguments[:3] == ["-m", "pip", "install"]:
    assert "--no-deps" in arguments and "--no-index" in arguments
    projects = [json.loads(Path(value).read_text()) for value in arguments if value.endswith(".whl")]
    (prefix / "extra-packages.json").write_text(json.dumps(projects))
elif arguments == ["-m", "pip", "check"]:
    pass
elif len(arguments) == 1 and Path(arguments[0]).name == "release_smoke.py":
    assert Path(arguments[0]).is_file()
else:
    sys.exit("Unsupported qualification command: " + repr(arguments))
