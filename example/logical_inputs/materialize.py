"""Write the same two intermediates from either representation of the document."""

import json
from pathlib import Path
import shutil
import sys
import zipfile


descriptor = json.loads(Path(sys.argv[1]).read_text())
files = descriptor["files"]
representation = descriptor["metadata"]["format"]
parts = ("heading", "body")
if representation == "files":
    for part in parts:
        shutil.copyfile(files[part], part + ".txt")
elif representation == "zip":
    with zipfile.ZipFile(files["archive"]) as archive:
        for part in parts:
            with archive.open(f"parts/{part}/text.txt") as source, open(part + ".txt", "wb") as output:
                shutil.copyfileobj(source, output)
else:
    raise ValueError(f"Unknown document representation: {representation!r}")
print(f"Materialized {representation} from {sys.argv[1]}")
