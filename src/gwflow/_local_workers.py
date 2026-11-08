"""gwf 3 scheduling with streaming logs and attested descendant termination.

Only this adapter depends on upstream's private scheduler hooks. Resource and
dependency scheduling stay upstream; execution and terminal evidence stay here.
"""

import asyncio
import logging
import os
import signal
import sys
import tempfile

from gwf.backends.exceptions import BackendError
from gwf.backends.local import LocalStatus, Scheduler, Server, get_socket_path
from gwf.log_storage import get_log_paths, prepare_log_storage_for_target

from . import _worker_process
from ._local_termination import TerminationRecords

logger = logging.getLogger(__name__)


class ManagedScheduler(Scheduler):
    def __init__(self, working_dir, max_cores, max_memory):
        super().__init__(working_dir, max_cores, max_memory)
        self.started = set()
        self.records = TerminationRecords(self.working_dir)

    def enqueue_task(self, name, script, working_dir, time_limit, deps, cores=1, memory="1g"):
        # Invalidate old receipts before upstream can schedule the submission.
        self.records.record(name, LocalStatus.UNKNOWN)
        return super().enqueue_task(name, script, working_dir, time_limit, deps, cores, memory)

    async def cancel_task(self, tid):
        if tid not in self.tasks:
            raise BackendError(f"Unknown task {tid}.")
        target = self.tasks[tid]
        state = self.task_states[tid]
        if state == LocalStatus.SUBMITTED:
            target.cancel_requested = True
            self._finish_target(target, LocalStatus.CANCELLED)
            self._schedule()
        elif state == LocalStatus.RUNNING and not target.cancel_requested:
            target.cancel_requested = True
            # Before the coroutine starts, cancellation would bypass its cleanup.
            if tid in self.started:
                target.execution.cancel()
        # A response acknowledges the request; terminal state follows cleanup.

    def _finish_target(self, target, state):
        if target.done.done():
            return
        try:
            self.records.record(target.name, state)
        except Exception:
            logger.exception("Local job %s could not persist termination evidence", target.name)
            state = LocalStatus.UNKNOWN
        super()._finish_target(target, state)

    async def _run_target(self, target):
        self.started.add(target.tid)
        state = await self._execute_task(target)
        target.execution = None
        # Unknown supervision may still own running work: keep its reservation.
        if state != LocalStatus.UNKNOWN:
            self.available_cores += target.cores
            self.available_memory += target.memory
        self._finish_target(target, state)
        self._schedule()

    async def _execute_task(self, target):
        launch = process = None
        stdout = stderr = None
        outcome = LocalStatus.UNKNOWN
        try:
            if target.cancel_requested:
                raise asyncio.CancelledError
            prepare_log_storage_for_target(self.working_dir, target.name)
            stdout_path, stderr_path = get_log_paths(self.working_dir, target.name)
            stdout = stdout_path.open("wb")
            stderr = stderr_path.open("wb")
            with tempfile.NamedTemporaryFile(mode="w", prefix="gwflow_") as script:
                script.write(target.script)
                script.flush()
                environment = {**os.environ, "GWF_EXEC_WORKFLOW_ROOT": str(self.working_dir)}
                # The supervisor owns the upstream runner and all executor children.
                # Shield spawn so cancellation cannot orphan a just-created process.
                launch = asyncio.create_task(asyncio.create_subprocess_exec(
                    sys.executable, "-m", "gwflow._worker_process", script.name,
                    cwd=target.working_dir, env=environment, stdin=asyncio.subprocess.PIPE,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                ))
                process = await asyncio.shield(launch)
                await asyncio.wait_for(process.wait(), timeout=target.time_limit)
            if process.returncode in _worker_process.CONFIRMED_TERMINATION:
                outcome = LocalStatus.COMPLETED if process.returncode == _worker_process.COMPLETED else LocalStatus.FAILED
        except (asyncio.CancelledError, TimeoutError) as error:
            # Timeout owns cleanup too. Later cancellation or shutdown must
            # acknowledge that request without interrupting its attestation.
            target.cancel_requested = True
            if launch is not None:
                process = await launch
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                await process.wait()
            if process is None or process.returncode in _worker_process.CONFIRMED_TERMINATION:
                outcome = LocalStatus.CANCELLED if isinstance(error, asyncio.CancelledError) else LocalStatus.KILLED
        except Exception:
            logger.exception("Local job %s could not establish a terminal outcome", target.name)
        finally:
            if process is not None and process.stdin is not None:
                process.stdin.close()
            for stream in (stdout, stderr):
                if stream is not None:
                    stream.close()
        return outcome


class ManagedServer(Server):
    async def send_response(self, writer, kind, **kwargs):
        if kind == "task_states":
            kwargs["gwflow_process_tree"] = 1
            kwargs["gwflow_submissions"] = {tid: target.name for tid, target in self.scheduler.tasks.items()}
            kwargs["gwflow_termination_records"] = 1
        await super().send_response(writer, kind, **kwargs)


async def serve(working_dir, max_cores, max_memory):
    scheduler = ManagedScheduler(working_dir, max_cores, max_memory)
    server = ManagedServer(scheduler)
    serving = asyncio.create_task(server.start_server(get_socket_path(working_dir)))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, serving.cancel)
    await serving
