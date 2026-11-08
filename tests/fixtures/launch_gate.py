"""Hold the OS subprocess-launch response after a real supervisor has spawned.

Installed as sitecustomize for a worker only. This controls the asynchronous
process boundary; it does not alter scheduler methods or process ownership.
"""

import asyncio
import os
from pathlib import Path


spawn = asyncio.create_subprocess_exec


async def held_spawn(*args, **kwargs):
    process = await spawn(*args, **kwargs)
    root = Path(os.environ["GWFLOW_LAUNCH_GATE"])
    if ("gwflow._worker_process" in args and "sample__right__" in Path(args[-1]).read_text()
            and not (root / "launch-held").exists()):
        (root / "launch-held").write_text(str(process.pid))
        while not (root / "launch-release").exists():
            await asyncio.sleep(0.025)
    return process


asyncio.create_subprocess_exec = held_spawn
