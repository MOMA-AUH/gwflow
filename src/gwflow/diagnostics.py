"""Command errors retain useful context and the original unexpected traceback."""

import traceback

import click


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
    click.echo("Error: " + error_message(error, summary), err=True, color=False)


class CommandGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.Abort):
            raise
        except Exception as error:
            raise click.ClickException(error_message(error, ctx.invoked_subcommand or "command")) from error
