"""Process-level startup boilerplate shared by every bridge ``main()``.

Each bridge independently repeated the same two startup snippets — a
``logging.basicConfig`` + named logger, and an optional GitHub
error-reporter registration. They live here so a bridge's ``main`` reads
as intent rather than copy-pasted plumbing.
"""

from __future__ import annotations

import logging


def configure_logging(app_name: str, level: str = "INFO") -> logging.Logger:
    """Apply the bridges' standard logging config and return the app logger.

    ``level`` is a level *name* (``"INFO"``, ``"DEBUG"``, …); an
    unrecognised value falls back to ``INFO``, matching the
    ``getattr(logging, LOG_LEVEL, logging.INFO)`` idiom every bridge used.
    The format is the uniform ``%(asctime)s %(levelname)s %(message)s``.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger(app_name)


def register_github_error_reporter(app_name: str) -> None:
    """Register the optional GitHub error reporter for *app_name*.

    A no-op when ``python_github_error_reporter`` isn't installed (e.g.
    local dev where ``GITHUB_ERROR_TOKEN`` is unset). Every bridge
    registered the same way: synchronous hook only
    (``install_async_hook=False``).
    """
    try:
        from python_github_error_reporter import GitHubErrorReporter

        GitHubErrorReporter.register(app=app_name, install_async_hook=False)
    except ImportError:
        pass
