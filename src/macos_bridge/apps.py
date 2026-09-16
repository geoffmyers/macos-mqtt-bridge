"""Resolve macOS bundle IDs to user-facing app names.

The "friendly" name is the .app directory's filename without the .app
extension — this is what the user sees in Finder, Dock, and Cmd-Tab.
For VS Code the .app is "Visual Studio Code.app" while
CFBundleName is "Code"; we prefer the former since that's the
Finder-visible name.

Resolution order:
  1. ``_FRIENDLY_OVERRIDES`` — hand-curated map for bundle IDs whose
     ``.app`` filename is a code-style token (e.g. ``bzbmenu``) rather
     than the user-recognized product name.
  2. ``mdfind "kMDItemCFBundleIdentifier == '<id>'"`` → first .app path,
     return basename minus ``.app``.
  3. Fall back to the bundle ID itself if mdfind returns no match.

Cached: bundle IDs don't change at runtime, so successful resolutions
are memoized for the process lifetime. Failures (mdfind timeouts or
no match) are deliberately NOT cached so a transient Spotlight hiccup
on first lookup doesn't permanently pin a bundle ID like
``com.microsoft.VSCode`` instead of ``Visual Studio Code``.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

# Hand-curated friendly names for bundle IDs that mdfind resolves to a
# code-style filename (e.g. com.backblaze.bzbmenu → "bzbmenu.app" → "bzbmenu",
# but the user knows it as "Backblaze Menu"). Override values WIN over mdfind
# resolution and bypass the cache lookup.
_FRIENDLY_OVERRIDES: dict[str, str] = {
    "com.backblaze.bzbmenu": "Backblaze Menu",
    "com.apple.loginwindow": "Login Window",
}

_resolved: dict[str, str] = {}


def bundle_id_to_app_name(bundle_id: str) -> str:
    """Return the Finder-visible app name for a macOS bundle ID,
    or the bundle ID unchanged if no matching .app is found.
    """
    if not bundle_id:
        return bundle_id
    override = _FRIENDLY_OVERRIDES.get(bundle_id)
    if override is not None:
        return override
    cached = _resolved.get(bundle_id)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("mdfind failed for %s: %s", bundle_id, exc)
        return bundle_id

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.endswith(".app"):
            name = line.rsplit("/", 1)[-1].removesuffix(".app")
            _resolved[bundle_id] = name
            return name
    return bundle_id


def _cache_clear() -> None:
    """Clear the resolved-name cache (used by tests)."""
    _resolved.clear()


class _CacheShim:
    """Back-compat shim so ``bundle_id_to_app_name.cache_clear()`` still works
    in tests that were written against the old ``@lru_cache``-decorated form.
    """

    @staticmethod
    def cache_clear() -> None:
        _cache_clear()


bundle_id_to_app_name.cache_clear = _CacheShim.cache_clear  # type: ignore[attr-defined]
