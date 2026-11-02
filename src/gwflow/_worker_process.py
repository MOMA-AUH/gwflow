"""Own one local job's descendants until none can continue computing."""

import select
import signal
import subprocess
import sys

from ._process_tree import become_subreaper, stop_descendants


# These exit codes attest that cleanup completed. An unhandled supervisor
# exception exits with 1, and cannot establish that its descendants stopped.
COMPLETED = 0
FAILED = 100
CANCELLED = 101
CONFIRMED_TERMINATION = frozenset((COMPLETED, FAILED, CANCELLED))


def main():
    # A separate subreaper per job prevents cancellation from touching siblings.
    become_subreaper()
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
            return CANCELLED
        process = subprocess.Popen(["/bin/sh", "-c", sys.argv[1]], stdin=subprocess.DEVNULL)
        while process.poll() is None:
            if stopping or select.select([sys.stdin], [], [], 0.025)[0]:
                return CANCELLED
        return COMPLETED if process.returncode == 0 else FAILED
    finally:
        stop_descendants()


if __name__ == "__main__":
    sys.exit(main())
