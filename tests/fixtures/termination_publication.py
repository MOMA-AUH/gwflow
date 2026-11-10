"""Gate or interrupt the real worker's atomic termination-ledger replacement."""

import json
import os
from pathlib import Path
import time


original_replace = os.replace
root = Path(os.environ['GWFLOW_TERMINATION_GATE'])


def replace(source, destination, **kwargs):
    if destination == 'records.json' and not (root / 'publication-held').exists():
        fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get('src_dir_fd'))
        with os.fdopen(fd) as stream:
            record = json.load(stream)
        if any(name.startswith('sample__gwflow_complete__') and state == 'COMPLETED'
               for name, state in record['submissions'].items()):
            (root / 'publication-held').touch()
            deadline = time.monotonic() + 60
            while not (root / 'publication-release').exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('Timed out waiting for publication release')
                time.sleep(.025)
    return original_replace(source, destination, **kwargs)


os.replace = replace
