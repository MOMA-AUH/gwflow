"""The dedicated managed-workflow executable, using gwf's backend services."""

from importlib.metadata import version
import logging
import os
from pathlib import Path
import time

import click
from gwf.backends import guess_backend
from gwf.conf import FileConfig
from gwf.plugins.config import config
from gwf.utils import entry_points, find_workflow

from ._frontend import ManagedContext
from .activity import DiagnosticHandler
from .cancel import cancel
from .clean_work import clean_work
from .diagnostics import CommandGroup
from .logs import logs
from .recover import recover
from .run import run
from .status import status
from .workers import workers
from .wait import wait


@click.group(cls=CommandGroup)
@click.version_option(version=version("gwflow"), prog_name="gwflow")
@click.option("-f", "--file", default="workflow.py:gwf", help="Workflow file and optional :OBJECT to load.")
@click.option("-b", "--backend", type=click.Choice(sorted({ep.name for ep in entry_points(group="gwf.backends")})),
              help="Backend used to run workflow.")
@click.option("-v", "--verbose", type=click.Choice(("warning", "debug", "info", "error")), default="info")
@click.pass_context
def main(ctx, file, backend, verbose):
    """Run and inspect managed Tasks. Use COMMAND --help for command options.

    NO_COLOR disables static colors while retaining layout and bars. --verbose
    controls diagnostic logging; --details controls command report depth.
    """
    if ctx.invoked_subcommand == 'wait':
        # Its disposable inspection owns configuration and backend loading too,
        # so even blocked command startup remains interruptible by the parent.
        ctx.meta['wait_started'] = time.monotonic()
        return
    # Importing gwf.cli executes backend and command plugins. Waiting owns that
    # work in its inspection child, including any descendants the imports start.
    from gwf.cli import LOGGING_FORMATS, get_level

    logging.basicConfig(level=get_level(verbose), format=LOGGING_FORMATS[verbose], handlers=[DiagnosticHandler()])
    try:
        path, obj_name = find_workflow(file)
    except FileNotFoundError:
        filename, _, obj_name = file.partition(":")
        path, obj_name = Path(filename).absolute(), obj_name or "gwf"
    working_dir = path.parent if path.is_file() else Path.cwd()
    settings = FileConfig.load(working_dir / ".gwfconf.json")
    # Keep explicit settings writable and leave ordinary gwf's defaults alone.
    settings.data.maps.insert(1, {"use_spec_hashes": True})
    if os.getenv("NO_COLOR"):
        ctx.color = False
    backend = backend or settings.get("backend") or guess_backend()[1]
    ctx.obj = ManagedContext(working_dir=str(working_dir), config=settings, backend=backend,
                             workflow_file=path, workflow_obj=obj_name)


for command in (run, status, logs, cancel, recover, clean_work, config, workers, wait):
    main.add_command(command)
