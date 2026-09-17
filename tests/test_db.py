import sqlite3
import threading
from pathlib import Path

import pytest

from macos_bridge.db import read_only_copy


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    """Create a small source SQLite DB with a known row, plus a fake -wal sidecar."""
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'alpha'), (2, 'beta')")
    conn.commit()
    conn.close()
    # Create a placeholder WAL sidecar so we exercise that code path
    (tmp_path / "source.db-wal").write_bytes(b"")
    (tmp_path / "source.db-shm").write_bytes(b"")
    return db_path


def test_read_only_copy_returns_connection_with_correct_data(source_db: Path, tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with read_only_copy(source_db, work_dir) as conn:
        rows = conn.execute("SELECT id, name FROM t ORDER BY id").fetchall()
    assert rows == [(1, "alpha"), (2, "beta")]


def test_read_only_copy_cleans_up_temp_files(source_db: Path, tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with read_only_copy(source_db, work_dir) as conn:
        conn.execute("SELECT 1").fetchone()
    # All copies should be deleted after exit
    assert list(work_dir.iterdir()) == []


def test_read_only_copy_blocks_writes(source_db: Path, tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with read_only_copy(source_db, work_dir) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (3, 'gamma')")


def test_read_only_copy_raises_if_source_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        with read_only_copy(tmp_path / "does-not-exist.db", tmp_path):
            pass


def test_read_only_copy_consistent_snapshot_under_concurrent_wal_writer(tmp_path: Path):
    """Regression test for the sqlite online-backup-API snapshot: a WAL-mode
    source with a background thread continuously inserting + committing
    exercises exactly the race a plain ``shutil.copy2`` of the main file
    plus its -wal/-shm sidecars cannot protect against (the three files
    could be copied at slightly different moments relative to the writer).

    Every insert into `t` is paired, in the SAME commit, with an increment
    of a trigger-maintained `running_count` in `meta`. A snapshot that is
    NOT transactionally consistent — old bytes for one table mixed with
    newer bytes for the other — would break the invariant
    ``COUNT(*) FROM t == running_count``. A consistent snapshot, taken at
    any point in time, always satisfies it.
    """
    db_path = tmp_path / "wal_source.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("CREATE TABLE meta (running_count INTEGER)")
    conn.execute("INSERT INTO meta VALUES (0)")
    conn.execute(
        "CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN "
        "UPDATE meta SET running_count = running_count + 1; END"
    )
    conn.commit()
    conn.close()

    stop = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        wconn = sqlite3.connect(db_path, timeout=5.0)
        try:
            i = 0
            while not stop.is_set():
                i += 1
                wconn.execute("INSERT INTO t (note) VALUES (?)", (f"fixture-row-{i}",))
                wconn.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            wconn.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    snapshots_taken = 0
    try:
        for _ in range(20):
            with read_only_copy(db_path, work_dir) as ro_conn:
                row_count, running_count = ro_conn.execute(
                    "SELECT (SELECT COUNT(*) FROM t), (SELECT running_count FROM meta)"
                ).fetchone()
                assert row_count == running_count, (
                    f"inconsistent snapshot: {row_count} rows in t but "
                    f"running_count={running_count}"
                )
                snapshots_taken += 1
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not errors, f"concurrent writer thread hit errors: {errors}"
    assert snapshots_taken == 20
