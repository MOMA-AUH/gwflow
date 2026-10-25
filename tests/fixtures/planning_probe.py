"""Count real I/O while invoking the installed CLI; never replace planner code.

Usage: python planning_probe.py report.json status [CLI arguments...]
Counts are attempted OS calls and Python stream reads, not kernel syscalls.
Phase times include instrumentation overhead and are not latency benchmarks.
json_bytes counts UTF-8 input bytes consumed by JSON decoders, not physical disk
traffic. RSS is the process high-water mark in KiB on Linux. Set
GWFLOW_PROBE_MEMORY=1 for optional Python allocation tracing (with extra overhead).
"""

from collections import Counter, defaultdict
import io
import json
import os
from pathlib import Path
import sys
import resource
import tracemalloc
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
        self.document_decodes = Counter()
        self.peak_cached_documents = 0

    def switch(self, phase):
        now = time.perf_counter()
        self.seconds[self.phase] += now - self.changed
        self.changed, self.phase = now, phase

    def profile(self, frame, event, arg):
        if event == "call":
            module = frame.f_globals.get("__name__", "")
            name = frame.f_code.co_name
            if module == "json" and name == "loads":
                payload = frame.f_locals["s"]
                self.counts[self.phase]["json_decodes"] += 1
                self.counts[self.phase]["json_bytes"] += len(payload.encode("utf-8") if isinstance(payload, str) else payload)
                caller = frame.f_back
                if caller.f_globals.get("__name__") == "json" and caller.f_code.co_name == "load":
                    stream = caller.f_locals["fp"]
                    try:
                        path = self.fds.get(stream.fileno(), str(getattr(stream, "name", "unknown")))
                    except (OSError, ValueError, io.UnsupportedOperation):
                        path = str(getattr(stream, "name", "unknown"))
                    if path.endswith("/record-state.json"):
                        self.document_decodes[path] += 1
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
            elif module == "gwflow.images" and name == "_pull":
                phase = "image_acquisition"
            elif name == "<module>":
                phase = "construction" if Path(frame.f_code.co_filename).name == "workflow.py" else "imports"
            if phase is not None:
                self.frames[id(frame)] = self.phase
                self.switch(phase)
        elif (event == "return" and frame.f_globals.get("__name__") == "gwflow._records"
              and frame.f_code.co_name == "read" and type(frame.f_locals.get("self")).__name__ == "_Documents"):
            self.peak_cached_documents = max(self.peak_cached_documents, len(frame.f_locals["self"].values))
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
            if name in ("open", "dup"):
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
        for name in ("fstat", "open", "dup", "close", "stat", "lstat", "readlink", "listdir", "read", "access"):
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
                "document_decodes": dict(self.document_decodes),
                "peak_cached_documents": self.peak_cached_documents,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "traced_memory_bytes": dict(zip(("current", "peak"), tracemalloc.get_traced_memory())) if tracemalloc.is_tracing() else None,
                "peak_opened_descriptors": self.peak, "path_depth": len(Path.cwd().parts) - 1}


if __name__ == "__main__":
    output, *arguments = sys.argv[1:]
    if os.environ.get("GWFLOW_PROBE_MEMORY"):
        tracemalloc.start()
    probe = Probe()
    try:
        with probe:
            from gwflow.cli import main
            main(arguments, standalone_mode=False)
    finally:
        Path(output).write_text(json.dumps(probe.report(), indent=2) + "\n")
