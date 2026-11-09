"""Pause a real supervisor during timeout cleanup, before it can attest exit."""

import os
from pathlib import Path
import signal
import time


root = Path(__file__).parent
(root / "gate-pid").write_text(str(os.getpid()))
pid = os.getpid()
while pid > 1:
    process = Path(f'/proc/{pid}')
    if b'gwflow._worker_process' in process.joinpath('cmdline').read_bytes().split(b'\0'):
        break
    pid = int(process.joinpath('stat').read_text().rsplit(')', 1)[1].split()[1])
else:
    raise RuntimeError('No managed supervisor in computation ancestry')
(root / 'supervisor-pid').write_text(str(pid))
held = False


def hold_cleanup(signum, frame):
    global held
    if not held:
        held = True
        os.kill(pid, signal.SIGSTOP)
        (root / 'cleanup-held').touch()


signal.signal(signal.SIGTERM, hold_cleanup)
(root / 'gate-started').touch()
while True:
    time.sleep(0.025)
