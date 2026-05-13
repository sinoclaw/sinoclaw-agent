"""Resolve SINOCLAW_HOME for standalone skill scripts.

Skill scripts may run outside the Sinoclaw process (e.g. system Python,
nix env, CI) where ``sinoclaw_constants`` is not importable.  This module
provides the same ``get_sinoclaw_home()`` and ``display_sinoclaw_home()``
contracts as ``sinoclaw_constants`` without requiring it on ``sys.path``.

When ``sinoclaw_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``sinoclaw_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``SINOCLAW_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from sinoclaw_constants import display_sinoclaw_home as display_sinoclaw_home
    from sinoclaw_constants import get_sinoclaw_home as get_sinoclaw_home
except (ModuleNotFoundError, ImportError):

    def get_sinoclaw_home() -> Path:
        """Return the Sinoclaw home directory (default: ~/.sinoclaw).

        Mirrors ``sinoclaw_constants.get_sinoclaw_home()``."""
        val = os.environ.get("SINOCLAW_HOME", "").strip()
        return Path(val) if val else Path.home() / ".sinoclaw"

    def display_sinoclaw_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``sinoclaw_constants.display_sinoclaw_home()``."""
        home = get_sinoclaw_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
