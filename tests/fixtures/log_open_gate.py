"""Pause a CLI log open at Python's filesystem audit boundary."""

import os
import sys
import time


held = False


def hold_log_open(event, args):
    global held
    if held or event != "open" or not isinstance(args[0], str):
        return
    name = os.path.basename(args[0])
    if not name.startswith("sample__right") or not name.endswith(os.environ["LOG_OPEN_STREAM"]):
        return
    held = True
    with open("log-open-held", "w") as marker:
        marker.write(args[0])
    deadline = time.monotonic() + 30
    while not os.path.exists("log-open-release"):
        if time.monotonic() >= deadline:
            raise TimeoutError("Test did not release the log open")
        time.sleep(0.01)


sys.addaudithook(hold_log_open)
