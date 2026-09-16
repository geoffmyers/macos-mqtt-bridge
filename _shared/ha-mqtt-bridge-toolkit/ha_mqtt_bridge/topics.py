"""MQTT topic + identifier string builders.

Pure string functions. Intentionally minimal: enough to cover the
bridges' topic conventions without imposing a particular namespace shape
on future bridges.
"""

from __future__ import annotations

import re
import socket

# Matches anything that isn't an MQTT-safe identifier char. We collapse
# runs of these to a single underscore so "Alex's iMac (Pro)" becomes
# "alex_s_imac_pro" instead of "alex_s_imac__pro_". MQTT topics
# themselves accept many of these chars, but unique_ids and HA entity
# slugs are stricter — and a single rule that works for both is easier
# than two rules that disagree at the edges.
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, replace non-alphanumeric runs with ``_``, strip leading
    and trailing underscores.

    Designed to be deterministic and idempotent: ``slugify(slugify(x)) ==
    slugify(x)``. Returns an empty string if ``text`` is empty or contains
    no alphanumeric chars.
    """
    if not text:
        return ""
    return _NON_SLUG_CHARS.sub("_", text.lower()).strip("_")


def slugify_hostname(hostname: str | None = None) -> str:
    """Slugify the local hostname (or a supplied one).

    Strips any DNS suffix (``"mac.local"`` → ``"mac"``) before
    slugifying so the result matches what each bridge has historically
    used as its host slug.
    """
    if hostname is None:
        hostname = socket.gethostname()
    base = hostname.split(".")[0]
    return slugify(base)


def state_topic(prefix: str, host_slug: str | None, suffix: str) -> str:
    """Build ``<prefix>/<host>/<suffix>`` (or ``<prefix>/<suffix>`` when
    ``host_slug`` is None).

    ``suffix`` may contain slashes — it's not slugified. The function
    only joins the three segments. Empty segments are dropped so callers
    can pass ``""`` interchangeably with ``None`` for the host portion.
    """
    parts = [p for p in (prefix, host_slug, suffix) if p]
    return "/".join(parts)


def discovery_topic(
    discovery_prefix: str, component: str, unique_id: str
) -> str:
    """Build ``<discovery_prefix>/<component>/<unique_id>/config``.

    ``unique_id`` may contain a single ``/`` segment (HA accepts up to
    one node-id / object-id pair) and is otherwise passed through
    unchanged. The caller is responsible for ensuring it matches the
    ``unique_id`` field in the payload.
    """
    return f"{discovery_prefix}/{component}/{unique_id}/config"


def event_topic(prefix: str, host_slug: str | None, *segments: str) -> str:
    """Build ``<prefix>/<host>/<segment1>/<segment2>/...``.

    Convenience for bridges that publish event topics with multiple
    path segments (macOS comms event sources). Empty segments and a
    ``None`` host are dropped, same rules as :func:`state_topic`.
    """
    parts = [prefix]
    if host_slug:
        parts.append(host_slug)
    parts.extend(s for s in segments if s)
    return "/".join(parts)
