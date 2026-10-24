"""``export-events``: the comms events of the last N days, exactly as the live bridge publishes them,
as JSONL, for Intake's one-time backfill. Read-only: each source runs its own ``poll()`` from the last
row before the cutoff with a throwaway in-memory state; ``state.json`` and the broker are never touched."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable

from macos_bridge.state import State


def export_events(sources: Iterable, cutoff_unix: float, topic_prefix: str, host_slug: str,
                  write: Callable[[str], object]) -> int:
    count = 0
    for source in sources:
        state = State("/dev/null")
        state.set(source.STATE_KEY, source.id_before(cutoff_unix))
        state.set(source.PRIMED_KEY, True)
        for event_path, payload in source.poll(state):
            clean = {"host": host_slug, **{k: v for k, v in payload.items() if not k.startswith("__")}}
            write(json.dumps({"topic": f"{topic_prefix}/{host_slug}/{event_path}", "payload": clean},
                             ensure_ascii=False) + "\n")
            count += 1
    return count
