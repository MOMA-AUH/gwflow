"""Keep Linux ownership of inspection descendants, including detached children.

The supervisor never imports authored workflow code. As a subreaper it adopts
orphaned descendants, so a new session or a double fork cannot escape cleanup.
Submitted workflow jobs belong to the backend's separate process tree.
"""

import signal
import subprocess
import sys
import time

from ._process_tree import become_subreaper, stop_descendants


def main():
    become_subreaper()
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
