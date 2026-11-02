"""Linux ownership and cleanup for an isolated supervisor's descendants."""

import ctypes
import os
from pathlib import Path
import signal
import time


def become_subreaper():
    # PR_SET_CHILD_SUBREAPER: retain descendants after their immediate parent exits.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def stop_descendants():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signaled: set[int] = set()
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
