"""Keep Linux ownership of inspection descendants, including detached children.

The supervisor never imports authored workflow code. As a subreaper it adopts
orphaned descendants, so a new session or a double fork cannot escape cleanup.
Submitted workflow jobs belong to the backend's separate process tree.
"""

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def stop_descendants():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signaled = set()
    deadline = time.monotonic() + 0.5
    while True:
        children = set(map(int, Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()))
        force = time.monotonic() >= deadline
        for pid in children if force else children - signaled:
            # These are our direct children and we have not reaped them yet:
            # their PIDs cannot be reused between enumeration and signaling.
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            signaled.add(pid)
        try:
            while pid := os.waitpid(-1, os.WNOHANG)[0]:
                signaled.discard(pid)
        except ChildProcessError:
            return
        # Orphaned grandchildren become direct children of this subreaper.
        time.sleep(0.025)


def main():
    # PR_SET_CHILD_SUBREAPER: retain descendants after their immediate parent exits.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    try:
        process = subprocess.Popen([sys.executable, "-m", sys.argv[1], *sys.argv[2:]])
        while process.poll() is None and not stopping:
            time.sleep(0.025)
        return 1 if stopping else process.returncode
    finally:
        stop_descendants()


if __name__ == "__main__":
    sys.exit(main())
