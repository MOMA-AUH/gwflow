"""Shared display capabilities for reports and frontend activity."""

import os
import fcntl


def supports_unicode(stream):
    try:
        "╭╮╰╯│─❯›○◷↻▶↗✓◇…✗⊘".encode(stream.encoding or "utf-8")
    except UnicodeEncodeError:
        return False
    return True


def supports_decoration(stream):
    return (stream.isatty() and supports_unicode(stream)
            and os.environ.get("TERM", "").lower() not in ("dumb", "unknown"))


def supports_keyboard(stream):
    try:
        return (stream is not None and stream.isatty()
                and fcntl.fcntl(stream.fileno(), fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY)
    except (OSError, ValueError):
        return False
