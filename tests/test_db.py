import sqlite3
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
