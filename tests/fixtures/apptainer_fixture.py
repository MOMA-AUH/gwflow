"""Controlled external Apptainer process for deterministic registry tests.

Only the process boundary is substituted; planning, jobs and files are real.
Real container execution is covered separately by the runtime acceptance suite.
"""

import json
import os
from pathlib import Path
import shutil
import sys


controller = Path(os.environ["GWFLOW_TEST_IMAGE_FIXTURE"])
options = json.loads((controller / "options.json").read_text())
arguments = sys.argv[1:]


def record(**fields):
    with (controller / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps(fields) + "\n")


if arguments[0] == "pull":
    destination, reference = Path(arguments[-2]), arguments[-1]
    record(operation="pull", reference=reference, destination=str(destination))
    destination.write_text("partial image")
    if options.get("fail"):
        sys.exit("fixture registry unavailable")
    if source := options.get("source"):
        shutil.copyfile(source, destination)
    else:
        destination.write_text(options.get("content", "fixture image\n"))
elif arguments[0] == "exec":
    command = arguments.index("/bin/bash")
    image = arguments[command - 1]
    record(operation="exec", image=image)
    environment = dict(os.environ, APPTAINER_CONTAINER=image)
    if temporary := environment.get("APPTAINERENV_TMPDIR"):
        environment["TMPDIR"] = temporary
    os.execvpe("/bin/bash", arguments[command:], environment)
else:
    sys.exit("unexpected fixture operation")
