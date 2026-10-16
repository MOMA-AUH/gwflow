"""The dedicated managed-workflow executable, using gwf's backend services."""

from importlib.metadata import version
import os
from pathlib import Path

import click
from gwf.backends import guess_backend, list_backends
from gwf.cli import configure_logging
from gwf.conf import FileConfig
from gwf.plugins.config import config
from gwf.utils import find_workflow

from ._frontend import ManagedContext
from .cancel import cancel
from .clean_work import clean_work
from .logs import logs
from .recover import recover
from .run import run
from .status import status
from .workers import workers


@click.group()
@click.version_option(version=version("gwflow"), prog_name="gwflow")
@click.option("-f", "--file", default="workflow.py:gwf", help="Workflow file and optional :OBJECT to load.")
@click.option("-b", "--backend", type=click.Choice(list_backends()), help="Backend used to run workflow.")
@click.option("-v", "--verbose", type=click.Choice(("warning", "debug", "info", "error")), default="info")
@click.option("--no-color/--use-color", default=None, help="Enable or disable output colors.")
@click.pass_context
def main(ctx, file, backend, verbose, no_color):
    """Run and inspect managed Tasks. Use COMMAND --help for command options."""
    configure_logging(level_name=verbose)
    try:
        path, obj_name = find_workflow(file)
    except FileNotFoundError:
        filename, _, obj_name = file.partition(":")
        path, obj_name = Path(filename).absolute(), obj_name or "gwf"
    working_dir = path.parent if path.is_file() else Path.cwd()
    settings = FileConfig.load(working_dir / ".gwfconf.json")
    # Keep explicit settings writable and leave ordinary gwf's defaults alone.
    settings.data.maps.insert(1, {"use_spec_hashes": True})
    if no_color is None:
        no_color = settings.get("no_color")
    if no_color is None:
        no_color = bool(os.getenv("NO_COLOR"))
    if no_color:
        ctx.color = False
    backend = backend or settings.get("backend") or guess_backend()[1]
    ctx.obj = ManagedContext(working_dir=str(working_dir), config=settings, backend=backend,
                             workflow_file=path, workflow_obj=obj_name)


for command in (run, status, logs, cancel, recover, clean_work, config, workers):
    main.add_command(command)
