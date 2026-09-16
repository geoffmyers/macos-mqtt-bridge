"""SQLite read helpers for Apple databases.

Two access patterns:

1. ``open_ro(path)`` — open the live DB read-only via URI mode=ro.
   Used for chat.db, CallHistory.storedata, and the FaceTime message
   store. Apple uses WAL, so concurrent readers are fine; we hold no
   writer lock and Apple's writer (Messages.app, etc.) is unaffected.

2. ``read_only_copy(source, work_dir)`` — copy the main DB plus its
   ``-wal`` and ``-shm`` sidecars into ``work_dir`` and open with
   ``mode=ro&immutable=1``. Robust against WAL state mutating between
   ticks while another process (CoreDuet, ScreenTimeAgent) is writing
   the live file. Used for knowledgeC.db and RMAdminStore-Local.sqlite,
   which see frequent in-flight writes that would otherwise produce
   sqlite3.OperationalError on every other tick.

A torn-snapshot crossing the copy window will raise on query rather
than silently returning corrupt data — acceptable for the polling
bridge use case.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


@contextmanager
def open_ro(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Read-only open of a live SQLite DB. Sets row_factory = sqlite3.Row."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"sqlite db not found: {p}")
    uri = f"file:{p.absolute()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def fetch_max(path: str | Path, table: str, column: str) -> int:
    """Return MAX(column) FROM table. Raises on error so callers can
    distinguish 'read failed' from 'table is empty' (both would otherwise
    return 0)."""
    with open_ro(path) as conn:
        row = conn.execute(f"SELECT MAX({column}) AS m FROM {table}").fetchone()
        return int(row["m"] or 0)


@contextmanager
def read_only_copy(source: Path, work_dir: Path) -> Iterator[sqlite3.Connection]:
    """Copy ``source`` (and its -wal/-shm sidecars if present) into ``work_dir``,
    yield a read-only sqlite3.Connection on the copy, delete the copy on exit.
    """
    if not source.exists():
        raise FileNotFoundError(source)

    work_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{source.name}.{uuid.uuid4().hex[:8]}"
    target_main = work_dir / stem
    target_wal = work_dir / f"{stem}-wal"
    target_shm = work_dir / f"{stem}-shm"

    shutil.copy2(source, target_main)
    src_wal = source.with_name(source.name + "-wal")
    if src_wal.exists():
        shutil.copy2(src_wal, target_wal)
    src_shm = source.with_name(source.name + "-shm")
    if src_shm.exists():
        shutil.copy2(src_shm, target_shm)

    try:
        uri = f"file:{target_main}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        for p in (target_main, target_wal, target_shm):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
