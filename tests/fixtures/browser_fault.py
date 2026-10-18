"""Raise from Textual's public key event hook to exercise CLI failure cleanup."""

from pathlib import Path
import runpy
import sys

from gwflow import browser


class FaultingBrowser(browser.StatusBrowser):
    def on_key(self, event):
        if event.key == "x":
            try:
                raise KeyError("original interface failure")
            except KeyError as error:
                raise RuntimeError("injected interface failure") from error


browser.StatusBrowser = FaultingBrowser
runpy.run_path(str(Path(sys.executable).with_name("gwflow")), run_name="__main__")
