"""Decode generic text using the descriptor delivered to the running computation."""

import gzip
import json
from pathlib import Path
import sys


root = Path(__file__).parent
descriptor = json.loads(Path(sys.argv[1]).read_text())
step = "join" if len(sys.argv) == 3 else "decode"
with (root / "executions.jsonl").open("a") as trace:
    trace.write(json.dumps({"step": step, "prefix": sys.prefix, "descriptor": descriptor}) + "\n")
if step == "join":
    if (root / "fail-join").exists():
        sys.exit("controlled join failure")
    sys.stdout.write(Path(sys.argv[2]).read_text())
source = Path(descriptor["files"]["source"])
if descriptor["metadata"]["encoding"] == "gzip":
    sys.stdout.write(gzip.decompress(source.read_bytes()).decode())
else:
    sys.stdout.write(source.read_text())
