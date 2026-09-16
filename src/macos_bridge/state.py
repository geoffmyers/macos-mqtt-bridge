"""Persistent last-seen IDs for each comms event source.

State file shape (additive):
{
  "messages_last_rowid": 123,
  "messages_primed": true,
  "calls_last_pk": 456,
  "calls_primed": true,
  "voicemail_last_pk": 789,
  "voicemail_primed": true
}

Phase tickers do not need persistent state — they always read the
current snapshot of knowledgeC.db / RMAdminStore-Local.sqlite.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


_DEFAULT_STATE: dict = {
    "messages_last_rowid": 0,
    "calls_last_pk": 0,
    "voicemail_last_pk": 0,
}


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.data = dict(_DEFAULT_STATE)
            return
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("could not read state file %s: %s — starting fresh", self.path, e)
            self.data = dict(_DEFAULT_STATE)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
