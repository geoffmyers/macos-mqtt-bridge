"""Spawns the Swift CallObserver binary and re-emits its JSON-line events.

Real-time alternative to the post-hoc `phone/started`/`ended` events from
CallHistory.storedata. Build the helper with
`helpers/call-observer/build.sh`; this class then keeps it running as a
subprocess.

The bridge runs the helper in its own thread so the polling loop is
unaffected. The thread restarts the helper if it exits, with a backoff to
avoid hammering when the binary is missing or permissions are blocked.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# Map of helper event names to event paths emitted by the bridge.
_EVENT_MAP = {
    "call_incoming_ringing": "phone/ringing",
    "call_outgoing_started": "phone/outgoing_started",
    "call_connected": "phone/connected",
    "call_ended": "phone/realtime_ended",
}


class RealtimeCallObserver:
    def __init__(
        self,
        binary_path: str,
        emit: Callable[[str, dict], None],
        *,
        restart_backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 60.0,
    ):
        self.binary_path = binary_path
        self.emit = emit
        self.restart_backoff_seconds = restart_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        if not Path(self.binary_path).exists():
            log.warning(
                "realtime call observer disabled — binary not found at %s "
                "(build with helpers/call-observer/build.sh)",
                self.binary_path,
            )
            return
        self._thread = threading.Thread(
            target=self._run, name="call-observer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _run(self) -> None:
        backoff = self.restart_backoff_seconds
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    [self.binary_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                    text=True,
                )
            except FileNotFoundError:
                log.error(
                    "call observer binary missing at %s — giving up",
                    self.binary_path,
                )
                return

            log.info("call observer started (pid=%s)", self._proc.pid)
            backoff = self.restart_backoff_seconds

            try:
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    self._handle_line(line)
                    if self._stop.is_set():
                        break
            except Exception:  # noqa: BLE001
                log.exception("call observer reader crashed")

            rc = self._proc.poll()
            log.warning("call observer exited rc=%s", rc)
            if self._stop.is_set():
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff_seconds)

    def _handle_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("non-JSON line from call observer: %s", line)
            return
        name = event.get("event")
        if name == "observer_started":
            log.info(
                "call observer ready (%d existing call(s))",
                event.get("existing_calls", 0),
            )
            return
        event_path = _EVENT_MAP.get(name)
        if event_path is None:
            log.debug("unrecognized call observer event %r", name)
            return
        payload = {k: v for k, v in event.items() if k != "event"}
        self.emit(event_path, payload)
