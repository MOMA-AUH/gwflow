"""Interrupt one supervisor launch at the worker's subprocess boundary."""

import asyncio
import errno
import os
from pathlib import Path
import select


spawn = asyncio.create_subprocess_exec

# Delay the real supervisor before its first instruction until the worker
# drops the control connection after losing the launch response.
if os.environ.get("GWFLOW_TEST_HOLD_SUPERVISOR"):
    select.select([0], [], [], 10)


async def controlled_spawn(*args, **kwargs):
    request = Path(os.environ["GWFLOW_SPAWN_FAILURE"])
    if ("gwflow._worker_process" not in args or not request.exists()
            or "bad__compute__" not in Path(args[-1]).read_text()):
        return await spawn(*args, **kwargs)

    mode = request.read_text()
    request.with_suffix(".held").touch()
    while request.exists():
        await asyncio.sleep(0.025)

    missing = request.with_suffix(".missing")
    if mode == "before":
        return await spawn(*args, executable=str(missing), **kwargs)
    if mode == "starting":
        kwargs["env"] = {**kwargs["env"], "GWFLOW_TEST_HOLD_SUPERVISOR": "1"}
        await spawn(*args, **kwargs)
    elif mode == "after":
        process = await spawn(*args, **kwargs)
        await process.wait()
    else:
        raise ValueError(mode)
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(missing))


asyncio.create_subprocess_exec = controlled_spawn
