"""SQLite read helpers for Apple databases.

Two access patterns:

1. ``open_ro(path)`` — open the live DB read-only via URI mode=ro.
   Used for chat.db, CallHistory.storedata, and the FaceTime message
   store. Apple uses WAL, so concurrent readers are fine; we hold no
   writer lock and Apple's writer (Messages.app, etc.) is unaffected.

2. ``read_only_copy(source, work_dir)`` — snapshot the live DB into
   ``work_dir`` and open the snapshot with ``mode=ro&immutable=1``.
   Used for knowledgeC.db and RMAdminStore-Local.sqlite, which see
   frequent in-flight writes that would otherwise produce
   sqlite3.OperationalError on every other tick.

   The snapshot is taken with SQLite's online backup API
   (``sqlite3.Connection.backup``), which is transactionally consistent
   even while another process (CoreDuet, ScreenTimeAgent) is actively
   writing the live WAL file — it holds its own read transaction and
   restarts the copy if the source changes underneath it, so the
   result is always a point-in-time snapshot, never a torn one. A
   plain ``shutil.copy2`` of the main file plus its ``-wal``/``-shm``
   sidecars (the previous, and now fallback-only, implementation)
   offered no such guarantee: the three files could be copied at
   slightly different moments relative to a concurrent writer.
   If the backup API raises (e.g. a permission quirk opening the
   source read-only), ``read_only_copy`` falls back to that plain file
   copy and logs a warning — best-effort, not guaranteed consistent.

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


def _backup_via_sqlite_api(source: Path, dest: Path) -> None:
    """Take a transactionally-consistent snapshot of ``source`` into ``dest``
    using SQLite's online backup API. Opens ``source`` read-only, so it never
    contends with, or is blocked by, the live writer."""
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=5.0)
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _copy_via_shutil(source: Path, target_main: Path, target_wal: Path, target_shm: Path) -> None:
    """Best-effort fallback: plain file copy of the main DB plus its -wal/-shm
    sidecars. Not guaranteed consistent against a concurrent writer — see the
    module docstring."""
    shutil.copy2(source, target_main)
    src_wal = source.with_name(source.name + "-wal")
    if src_wal.exists():
        shutil.copy2(src_wal, target_wal)
    src_shm = source.with_name(source.name + "-shm")
    if src_shm.exists():
        shutil.copy2(src_shm, target_shm)


@contextmanager
def read_only_copy(source: Path, work_dir: Path) -> Iterator[sqlite3.Connection]:
    """Snapshot ``source`` into ``work_dir``, yield a read-only
    sqlite3.Connection on the snapshot, delete the snapshot on exit.

    See the module docstring: the snapshot is taken with SQLite's online
    backup API for transactional consistency, falling back to a plain file
    copy only if that API raises.
    """
    if not source.exists():
        raise FileNotFoundError(source)

    work_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{source.name}.{uuid.uuid4().hex[:8]}"
    target_main = work_dir / stem
    target_wal = work_dir / f"{stem}-wal"
    target_shm = work_dir / f"{stem}-shm"

    try:
        _backup_via_sqlite_api(source, target_main)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "read_only_copy: sqlite backup API failed for %s (%s) — falling back "
            "to a file copy, which is not guaranteed consistent against a "
            "concurrent writer",
            source, e,
        )
        _copy_via_shutil(source, target_main, target_wal, target_shm)

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
