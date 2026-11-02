"""Own one local job's descendants until none can continue computing."""

import ctypes
import os
import select
import signal
import subprocess
import sys

from ._inspection_process import stop_descendants


def main():
    # A separate subreaper per job prevents cancellation from touching siblings.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # The worker keeps this pipe open. EOF requests termination, including
        # if the worker dies, without a signal racing supervisor initialization.
        if select.select([sys.stdin], [], [], 0)[0]:
            return 101
        process = subprocess.Popen(["/bin/sh", "-c", sys.argv[1]], stdin=subprocess.DEVNULL)
        while process.poll() is None:
            if stopping or select.select([sys.stdin], [], [], 0.025)[0]:
                return 101
        return 0 if process.returncode == 0 else 100
    finally:
        stop_descendants()


if __name__ == "__main__":
    sys.exit(main())
