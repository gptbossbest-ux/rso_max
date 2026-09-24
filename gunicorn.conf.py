"""Strict Gunicorn settings for the Flask web portal.

The web process does not start APScheduler; scheduled jobs remain in bot.py.
"""

from __future__ import annotations

import os


def _integer_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer between {low} and {high}") from exc
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


bind = "0.0.0.0:5000"
workers = _integer_env("WEB_WORKERS", 2, 1, 16)
threads = _integer_env("WEB_THREADS", 4, 1, 64)
timeout = _integer_env("WEB_TIMEOUT", 60, 10, 600)
