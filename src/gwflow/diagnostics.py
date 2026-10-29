"""Command errors retain useful context and the original unexpected traceback."""

import traceback

import click

from .activity import message


def error_message(error, summary):
    if isinstance(error, click.ClickException):
        return error.format_message()
    if isinstance(error, SyntaxError):
        return (f"could not load the workflow\n  {error.filename}:{error.lineno}\n"
                f"  {error.msg}\n  Correct this source location and try again.")
    if isinstance(error, OSError):
        return f"{error}\n  Check the affected resource and its permissions."
    return (f"{summary} failed unexpectedly\n  {type(error).__name__}: {error}\n\n"
            "Traceback:\n" + "".join(traceback.format_exception(error)))


def report_error(error, summary):
    """Report a caught failure without changing a command's partial receipts."""
    message("Error: " + error_message(error, summary), err=True)


class CommandGroup(click.Group):
    def invoke(self, ctx):
        arguments = list(ctx.args)
        try:
            return super().invoke(ctx)
        except (click.Abort, click.exceptions.Exit):
            raise
        except Exception as error:
            # Configuration and backend selection happen before status gets its
            # command context. Keep those caught loading failures structured too.
            if ctx.invoked_subcommand == "status":
                from .status_json import emit_error, json_requested
                if json_requested(arguments):
                    emit_error(error)
            if isinstance(error, click.ClickException):
                raise
            raise click.ClickException(error_message(error, ctx.invoked_subcommand or "command")) from error
