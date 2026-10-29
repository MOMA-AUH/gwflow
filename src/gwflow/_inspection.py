"""Own disposable inspections independently of their user interface."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


class InspectionProcess:
    """Own one acquisition and all subprocesses it creates, never workflow jobs."""

    def __init__(self, arguments, *, module="gwflow.status_snapshot", command="status", request=None):
        self.directory = tempfile.TemporaryDirectory(prefix="gwflow-status-")
        self.root = Path(self.directory.name)
        self.result = self.root / "snapshot.json"
        self.started = time.monotonic()
        if request is not None:
            (self.root / "request.json").write_text(json.dumps(request))
        with (self.root / "stdout").open("wb") as output, (self.root / "stderr").open("wb") as errors:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "gwflow._inspection_process", module, *arguments[0],
                 command, "--snapshot-file", str(self.result), "--", *arguments[1]],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True,
            )

    def phase(self):
        with (self.root / "stderr").open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 8192))
            lines = stream.read().decode(errors="replace").splitlines()
        return next((line for line in reversed(lines) if line.startswith(
            ("Inspecting ", "Preparing ", "Checking ", "Waiting ", "Pulling image:"))), "Loading workflow...")

    def completed(self):
        return self.process.poll() is not None

    def outcome(self):
        output = (self.root / "stdout").read_text(errors="replace")
        diagnostics = (self.root / "stderr").read_text(errors="replace")
        if self.process.returncode:
            return None, diagnostics or "Inspection failed without diagnostics.", output
        snapshot = json.loads(self.result.read_text())
        snapshot["inspection_output"] = output
        snapshot["inspection_diagnostics"] = diagnostics
        return snapshot, "", output

    def close(self):
        try:
            self.process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # The unreaped supervisor still owns this PID / process group.
            # Never signal its numeric group after wait() has released it.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
        self.directory.cleanup()

