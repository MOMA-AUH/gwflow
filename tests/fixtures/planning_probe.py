"""Count real I/O while invoking the installed CLI; never replace planner code.

Usage: python planning_probe.py report.json status [CLI arguments...]
Counts are attempted OS calls and Python stream reads, not kernel syscalls.
Phase times include instrumentation overhead and are not latency benchmarks.
"""

from collections import Counter, defaultdict
import io
import json
import os
from pathlib import Path
import sys
import time


class Probe:
    def __init__(self):
        self.phase = "startup"
        self.frames = {}
        self.counts = defaultdict(Counter)
        self.paths = defaultdict(Counter)
        self.seconds = Counter()
        self.changed = time.perf_counter()
        self.fds = {}
        self.peak = 0
        self.originals = {}

    def switch(self, phase):
        now = time.perf_counter()
        self.seconds[self.phase] += now - self.changed
        self.changed, self.phase = now, phase

    def profile(self, frame, event, arg):
        if event == "call":
            module = frame.f_globals.get("__name__", "")
            name = frame.f_code.co_name
            phase = None
            if module == "gwflow.planning" and name == "plan_workflow":
                phase = "planning"
            elif module.startswith("gwf.backends.") or module in ("state_backend", "recovery_backend"):
                phase = "backend"
                if ((module == "gwf.backends.local" and name == "status"
                     and type(frame.f_locals.get("self")).__name__ == "Client")
                        or module == "gwf.backends.slurm" and name == "get_job_states"
                        or module == "state_backend" and name == "status"):
                    self.counts[phase]["scheduler_requests"] += 1
            elif module == "gwflow.presentation":
                phase = "rendering"
            elif module == "gwflow.images" and name == "acquire":
                phase = "image_acquisition"
            elif name == "<module>":
                phase = "construction" if Path(frame.f_code.co_filename).name == "workflow.py" else "imports"
            if phase is not None:
                self.frames[id(frame)] = self.phase
                self.switch(phase)
        elif event == "return" and id(frame) in self.frames:
            self.switch(self.frames.pop(id(frame)))
        elif event == "c_call" and getattr(arg, "__name__", "") in ("read", "readinto", "readline"):
            stream = getattr(arg, "__self__", None)
            if isinstance(stream, io.IOBase):
                self.counts[self.phase]["stream_reads"] += 1
                try:
                    path = self.fds.get(stream.fileno())
                except (OSError, ValueError):
                    path = None
                if path:
                    self.paths[self.phase]["read:" + path] += 1

    def path(self, value, kwargs):
        if isinstance(value, int):
            return self.fds.get(value, f"fd:{value}")
        path = os.fsdecode(value)
        if not os.path.isabs(path):
            path = os.path.join(self.fds.get(kwargs.get("dir_fd"), os.getcwd()), path)
        return os.path.normpath(path)

    def operation(self, name):
        original = getattr(os, name)
        self.originals[name] = original

        def counted(*args, **kwargs):
            path = self.path(args[0], kwargs)
            category = ("directory_opens" if args[1] & os.O_DIRECTORY else "file_opens") if name == "open" else name
            self.counts[self.phase][category] += 1
            self.paths[self.phase][category + ":" + path] += 1
            result = original(*args, **kwargs)
            if name == "open":
                self.fds[result] = path
                # fdopen closes in C; remove stale descriptors before counting.
                for fd in list(self.fds):
                    try:
                        self.originals["fstat"](fd)
                    except OSError:
                        del self.fds[fd]
                self.peak = max(self.peak, len(self.fds))
            elif name == "close":
                self.fds.pop(args[0], None)
            return result

        setattr(os, name, counted)

    def __enter__(self):
        for name in ("fstat", "open", "close", "stat", "lstat", "readlink", "listdir", "read"):
            self.operation(name)
        sys.setprofile(self.profile)
        return self

    def __exit__(self, *exc):
        sys.setprofile(None)
        self.switch("finished")
        for name, original in self.originals.items():
            setattr(os, name, original)

    def report(self):
        return {"counts": dict(self.counts), "paths": dict(self.paths), "seconds": dict(self.seconds),
                "peak_opened_descriptors": self.peak, "path_depth": len(Path.cwd().parts) - 1}


if __name__ == "__main__":
    output, *arguments = sys.argv[1:]
    probe = Probe()
    try:
        with probe:
            from gwf.cli import main
            main(arguments, standalone_mode=False)
    finally:
        Path(output).write_text(json.dumps(probe.report(), indent=2) + "\n")
