"""
Sinoclaw CLI - Unified command-line interface for Sinoclaw Agent.

Provides subcommands for:
- sinoclaw chat          - Interactive chat (same as ./hermes)
- sinoclaw gateway       - Run gateway in foreground
- sinoclaw gateway start - Start gateway service
- sinoclaw gateway stop  - Stop gateway service
- sinoclaw setup         - Interactive setup wizard
- sinoclaw status        - Show status of all components
- sinoclaw cron          - Manage cron jobs
"""

import os
import sys

__version__ = "0.13.0"
__release_date__ = "2026.5.7"


def _ensure_utf8():
    """Force UTF-8 stdout/stderr on Windows to prevent UnicodeEncodeError.

    Windows services and terminals default to cp1252, which cannot encode
    box-drawing characters used in CLI output. This causes unhandled
    UnicodeEncodeError crashes on gateway startup.
    """
    if sys.platform != "win32":
        return
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            if getattr(stream, "encoding", "").lower().replace("-", "") != "utf8":
                new_stream = open(
                    stream.fileno(), "w", encoding="utf-8",
                    buffering=1, closefd=False,
                )
                setattr(sys, stream_name, new_stream)
        except (AttributeError, OSError):
            pass


_ensure_utf8()
