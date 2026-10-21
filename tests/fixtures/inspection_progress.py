"""Coordinate scheduler acquisition and a later Task inspection independently."""

from pathlib import Path
import os
import time

from gwf.exceptions import WorkflowError
from gwflow import planning


def install(root):
    root = Path(root)
    create_backend = planning.create_backend
    resolve = planning.images.resolve
    inspected = 0

    def wait(phase):
        if not (root / ('hold-' + phase)).exists():
            return
        (root / (phase + '-held')).write_text(str(os.getpid()))
        deadline = time.monotonic() + 30
        while not (root / (phase + '-release')).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('Timed out waiting for ' + phase)
            time.sleep(0.025)

    def backend(*args, **kwargs):
        wait('scheduler')
        return create_backend(*args, **kwargs)

    def images(*args, **kwargs):
        nonlocal inspected
        inspected += 1
        if inspected == 1 and (root / 'block-first').exists():
            raise WorkflowError('injected image unavailable')
        if inspected == 2:
            wait('task')
        return resolve(*args, **kwargs)

    planning.create_backend = backend
    planning.images.resolve = images
