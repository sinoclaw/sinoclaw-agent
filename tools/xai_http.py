"""Shared helpers for direct xAI HTTP integrations."""

from __future__ import annotations


def sinoclaw_xai_user_agent() -> str:
    """Return a stable Sinoclaw-specific User-Agent for xAI HTTP calls."""
    try:
        from sinoclaw_cli import __version__
    except Exception:
        __version__ = "unknown"
    return f"Sinoclaw-Agent/{__version__}"
