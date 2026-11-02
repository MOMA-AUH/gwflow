"""Local scheduling with terminal states backed by process-tree termination."""

import asyncio
import json
import logging
from pathlib import Path
import signal
import sys

from gwf.backends.local import LocalStatus, Scheduler, Server

from . import _worker_process

logger = logging.getLogger(__name__)


class ManagedScheduler(Scheduler):
    def __init__(self, working_dir, max_cores):
        super().__init__(working_dir, max_cores)
        self.started = set()
        self.stopping = set()

    async def cancel_task(self, tid):
        if self.task_states.get(tid) in (LocalStatus.SUBMITTED, LocalStatus.RUNNING) and tid not in self.stopping:
            self.stopping.add(tid)
            if tid in self.started:
                self.tasks[tid].cancel()

    async def shutdown(self):
        for tid in self.tasks:
            await self.cancel_task(tid)
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def try_handle_task(self, tid, name, script, working_dir, time_limit, deps):
        self.started.add(tid)
        acquired = False
        launch = None
        process = None
        stdout = stderr = None
        outcome = LocalStatus.UNKNOWN
        try:
            if tid in self.stopping:
                raise asyncio.CancelledError
            if deps:
                await asyncio.wait({self.tasks[dependency] for dependency in deps})
                for dependency in deps:
                    if self.task_states[dependency] != LocalStatus.COMPLETED:
                        outcome = self.task_states[dependency]
                        return
            await self.cores_ressource.acquire()
            acquired = True
            self.task_states[tid] = LocalStatus.RUNNING
            # Shield spawn so cancellation cannot lose ownership of a process
            # created before asyncio hands its handle back to the scheduler.
            logs = Path(self.working_dir) / ".gwf" / "logs"
            stdout = (logs / f"{name}.stdout").open("wb")
            stderr = (logs / f"{name}.stderr").open("wb")
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-m", "gwflow._worker_process", script,
                cwd=working_dir, stdin=asyncio.subprocess.PIPE,
                stdout=stdout, stderr=stderr, start_new_session=True,
            ))
            process = await asyncio.shield(launch)
            await asyncio.wait_for(process.wait(), timeout=time_limit)
            # A signal-killed supervisor cannot attest that it reaped children.
            if process.returncode in _worker_process.CONFIRMED_TERMINATION:
                outcome = LocalStatus.COMPLETED if process.returncode == _worker_process.COMPLETED else LocalStatus.FAILED
        except (asyncio.CancelledError, TimeoutError) as error:
            if launch is not None:
                process = await launch
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                await process.wait()
            if process is None or process.returncode in _worker_process.CONFIRMED_TERMINATION:
                outcome = LocalStatus.CANCELLED if isinstance(error, asyncio.CancelledError) else LocalStatus.KILLED
        except Exception:
            logger.exception("Local job %s could not establish a terminal outcome", name)
        finally:
            if process is not None and process.stdin is not None:
                process.stdin.close()
            for stream in (stdout, stderr):
                if stream is not None:
                    stream.close()
            # This is the sole terminal-state publication, after owned work stops.
            self.task_states[tid] = outcome
            if acquired:
                self.cores_ressource.release()


class ManagedServer(Server):
    def __init__(self, scheduler):
        super().__init__(scheduler)
        self.connections = set()

    async def handle_connection(self, reader, writer):
        self.connections.add(writer)
        try:
            while data := await reader.readline():
                message = json.loads(data)
                kind = message.pop("__kind__")
                if kind == "enqueue_task":
                    tid = await self.scheduler.enqueue_task(**message)
                    await self.send_response(writer, "task_enqueued", tid=tid)
                elif kind == "get_task_states":
                    await self.send_response(writer, "task_states", tasks=self.scheduler.get_task_states())
                elif kind == "get_task_state":
                    await self.send_response(writer, "task_state", state=self.scheduler.get_task_state(message["tid"]))
                elif kind == "cancel_task":
                    await self.scheduler.cancel_task(message["tid"])
                elif kind == "shutdown":
                    self.server.close()
                    break
                elif kind == "close":
                    break
                else:
                    raise ValueError(f"Unknown local worker request: {kind}")
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.discard(writer)

    async def send_response(self, writer, kind, **kwargs):
        if kind == "task_states":
            kwargs["gwflow_process_tree"] = 1
        await super().send_response(writer, kind, **kwargs)


async def serve(working_dir, max_cores, host, port):
    scheduler = ManagedScheduler(working_dir, max_cores)
    server = ManagedServer(scheduler)
    serving = asyncio.create_task(server.start_server(host, port))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, serving.cancel)
    try:
        await serving
    finally:
        if server.server is not None:
            server.server.close()
        await scheduler.shutdown()
        for writer in tuple(server.connections):
            writer.close()
        if server.server is not None:
            await server.server.wait_closed()
