"""Owned process observations shared by installed termination journeys."""

import os
from pathlib import Path
import shlex
import signal


class ProcessProbe:
    def probe(self, *, escape=False):
        release = self.work / "release"
        self.addCleanup(release.touch)
        pids = self.work / "pids"

        def cleanup():
            if pids.exists():
                for pid in map(int, pids.read_text().split()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.addCleanup(cleanup)
        child = (f"echo $BASHPID >> {shlex.quote(str(pids))}; "
                 f"touch {shlex.quote(str(self.work / 'held'))}; "
                 f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.025; done; "
                 f"touch {shlex.quote(str(self.work / 'late-write'))}")
        launcher = "setsid /bin/bash" if escape else "/bin/bash"
        return (f"echo $BASHPID >> {shlex.quote(str(pids))}; "
                f"{launcher} -c {shlex.quote(child)} & wait; printf right > same.txt")

    def assert_probe_stopped(self):
        self.assertEqual(self.running_probe_processes(), [],
                         "Computation processes still run after confirmed inactivity")

    def running_probe_processes(self):
        running = []
        for pid in map(int, (self.work / "pids").read_text().split()):
            path = Path(f"/proc/{pid}/stat")
            try:
                state = path.read_text().rsplit(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                continue
            if state != "Z":
                running.append(pid)
        return running

    def supervisor_pid(self):
        pid = int((self.work / "pids").read_text().split()[0])
        while pid > 1:
            process = Path(f"/proc/{pid}")
            if b"gwflow._worker_process" in process.joinpath("cmdline").read_bytes().split(b"\0"):
                return pid
            pid = int(process.joinpath("stat").read_text().rsplit(")", 1)[1].split()[1])
        self.fail("No managed supervisor owns the foreground command")
