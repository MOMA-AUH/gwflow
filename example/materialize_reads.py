"""Prepare the selected read files, or verify materialized FASTQ mates."""

import hashlib
import json
from pathlib import Path
import sys
import tarfile


mode = sys.argv[1]
descriptor = json.loads(Path(sys.argv[2]).read_text())
format_name = descriptor["metadata"]["format"]
if mode == "prepare":
    # A single bundle keeps the target outputs stable across input formats.
    if format_name == "fastq":
        names = {"R1": "R1.fastq", "R2": "R2.fastq"}
    elif format_name == "spring":
        names = {"archive": "input.spring"}
    else:
        raise ValueError(f"Unknown read format: {format_name!r}")
    with tarfile.open("reads.tar", "w", dereference=True) as bundle:
        for key, name in names.items():
            bundle.add(descriptor["files"][key], arcname=name, recursive=False)
elif mode == "verify":
    for mate, filename in zip(("R1", "R2"), (sys.argv[3], sys.argv[4])):
        with Path(filename).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != descriptor["identity"][mate]:
                raise ValueError(f"{mate} differs from its recorded FASTQ checksum")
    message = f"Verified both FASTQ mates from {format_name}"
    Path("verified.txt").write_text(message + "\n")
    print(message)
else:
    raise ValueError(f"Unknown mode: {mode!r}")
