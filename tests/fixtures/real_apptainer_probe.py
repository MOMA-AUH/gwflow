"""Record real Apptainer calls; deny pulls after the acceptance cache is warm."""

import json
import os
from pathlib import Path
import platform
import subprocess
import sys


probe = Path(os.environ["GWFLOW_TEST_IMAGE_PROBE"])
arguments = sys.argv[1:]
record = {"arguments": arguments, "node": platform.node(), "architecture": platform.machine()}
if arguments[0] == "exec":
    image = Path(arguments[arguments.index("/bin/bash") - 1])
    info = image.stat()
    record["image"] = {"path": str(image), "resolved": str(image.resolve()),
                       "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                       "device": info.st_dev, "inode": info.st_ino}
with (probe / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(record) + "\n")
if arguments[0] == "pull" and (probe / "deny-pull").exists():
    sys.exit("Registry acquisition is disabled for warm-cache acceptance")
sys.exit(subprocess.run([os.environ["GWFLOW_TEST_REAL_APPTAINER"], *arguments]).returncode)
