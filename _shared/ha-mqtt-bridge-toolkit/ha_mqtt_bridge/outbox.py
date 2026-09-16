"""Disk-backed FIFO outbox for MQTT publishes.

Survives broker outages and process restarts. Every event is appended to a
JSONL file before publish is attempted; on successful broker ACK (paho's
wait_for_publish), the line is dropped from the file.

Cap on max retained lines prevents runaway growth if the broker stays down
forever.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)


class Outbox:
    def __init__(self, path: str | Path, max_pending: int = 50_000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_pending = max_pending
        self._lock = Lock()

    def enqueue(self, topic: str, payload: dict, *, retain: bool | None = None) -> None:
        record = {"topic": topic, "payload": payload}
        if retain is not None:
            record["retain"] = retain
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            if self._pending_count_unlocked() >= self.max_pending:
                log.error(
                    "outbox full (%d entries) — dropping event for %s",
                    self.max_pending, topic,
                )
                return
            with open(self.path, "a") as f:
                f.write(line + "\n")

    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count_unlocked()

    def _pending_count_unlocked(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path) as f:
            return sum(1 for _ in f)

    def drain(self, publish_fn: Callable[[str, dict, bool | None], bool]) -> int:
        """Try to publish every queued event in FIFO order.

        publish_fn(topic, payload, retain) -> True on broker ACK, False
        otherwise. ``retain`` is the per-event override (or None to use the
        publisher's default). Returns the number of events successfully drained.

        On any False, drain stops and the remaining queue is preserved
        (including the failing event, which will be retried on the next
        drain). Preserves ordering at the cost of head-of-line blocking
        when one event keeps failing.
        """
        with self._lock:
            if not self.path.exists():
                return 0
            with open(self.path) as f:
                lines = f.readlines()
            if not lines:
                return 0

            drained = 0
            failed_at: int | None = None
            for i, line in enumerate(lines):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("outbox: dropping corrupt line: %s", line.strip())
                    continue
                ok = False
                try:
                    ok = publish_fn(
                        event["topic"], event["payload"], event.get("retain")
                    )
                except Exception:  # noqa: BLE001
                    log.exception("outbox: publish_fn raised for %s", event.get("topic"))
                if ok:
                    drained += 1
                else:
                    failed_at = i
                    break

            if failed_at is None:
                self.path.write_text("")
            elif drained > 0:
                tail = lines[failed_at:]
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text("".join(tail))
                os.replace(tmp, self.path)

            return drained
