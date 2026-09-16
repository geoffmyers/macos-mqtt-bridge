"""Production error reporter: sends uncaught exceptions to a GitHub
repository as ``repository_dispatch`` events, at most once per unique error
within the deduplication window.
"""

from .reporter import (
    GitHubErrorReporter,
    register,
    report,
    report_async,
)

__all__ = [
    "GitHubErrorReporter",
    "register",
    "report",
    "report_async",
]
