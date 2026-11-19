"""Exit a submitting frontend after the first actual write of tracking JSON."""

import json
import os
from pathlib import Path
import sys

from gwflow.cli import main


phase = sys.argv.pop(1)
sys.argv[0] = 'gwflow'
original_dump = json.dump
backend = sys.argv[sys.argv.index('-b') + 1]
directory = Path.cwd() / '.gwf'
previous = json.loads((directory / f'{backend}-backend-tracked.json').read_text())


def dump(value, stream, **kwargs):
    # Restrict the fault to workflow-wide tracking and its following status
    # cache by recognizing the first Task's actual backend IDs. A spec-hash
    # document also contains the Submission names, but holds different values.
    path = Path(os.readlink(f'/proc/self/fd/{stream.fileno()}'))
    if path.parent == directory and isinstance(value, dict):
        if phase == 'tracking':
            selected = (all(value.get(name) == job_id for name, job_id in previous.items())
                        and any(name.startswith('b__') for name in value))
        else:
            selected = bool(set(previous.values()).intersection(value))
        if selected:
            class InterruptedWriter:
                def write(self, chunk):
                    stream.write(chunk)
                    stream.flush()
                    os._exit(119)
            return original_dump(value, InterruptedWriter(), **kwargs)
    return original_dump(value, stream, **kwargs)


json.dump = dump
main()
