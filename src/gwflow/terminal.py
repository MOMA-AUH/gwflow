"""Shared display capabilities for reports and frontend activity."""

import os


def supports_unicode(stream):
    try:
        "╭╮╰╯│─━╸╺○◷↻▶↗✓◇…✗⊘".encode(stream.encoding or "utf-8")
    except UnicodeEncodeError:
        return False
    return True


def supports_decoration(stream):
    return (stream.isatty() and supports_unicode(stream)
            and os.environ.get("TERM", "").lower() not in ("dumb", "unknown"))
