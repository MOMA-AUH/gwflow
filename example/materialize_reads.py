"""Write and verify the two FASTQ mates described by a gwflow logical input."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


descriptor = json.loads(Path(sys.argv[1]).read_text())
files = descriptor["files"]
format_name = descriptor["metadata"]["format"]
mates = {"R1": Path("R1.fastq"), "R2": Path("R2.fastq")}
if format_name == "fastq":
    for mate, output in mates.items():
        shutil.copyfile(files[mate], output)
elif format_name == "spring":
    # SPRING 1.1.1 passes the archive name to tar without quoting spaces.
    Path("input.spring").symlink_to(files["archive"])
    subprocess.run(["spring", "-d", "-t", "1", "-i", "input.spring",
                    "-o", mates["R1"], mates["R2"]], check=True)
else:
    raise ValueError(f"Unknown read format: {format_name!r}")

for mate, output in mates.items():
    with output.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != descriptor["identity"][mate]:
            raise ValueError(f"{mate} differs from its recorded FASTQ checksum")
print(f"Verified both FASTQ mates from {format_name}")
