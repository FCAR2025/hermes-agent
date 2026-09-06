from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway.kanban_snapshot import (
    InvalidKanbanIdentifier,
    KanbanBoardNotFound,
    KanbanTaskNotFound,
    read_kanban_task_snapshot,
)


def _create_board(root: Path, board: str, task_id: str = "t_123") -> Path:
    board_dir = root / "kanban" / "boards" / board
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(
        '{"slug":"' + board + '"}\n', encoding="utf-8"
    )
    db_path = board_dir / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (task_id, "review"))
    conn.execute(
        "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)",
        (task_id, "created", 1),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)",
        (task_id, "status", 2),
    )
    conn.commit()
    conn.close()
    return db_path


def _seeded(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    return _create_board(tmp_path, "fcar-board")


def test_valid_snapshot_is_exact_and_read_only(tmp_path, monkeypatch):
    db_path = _seeded(tmp_path, monkeypatch)
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in db_path.parent.iterdir()
        if p.is_file()
    }

    snapshot = read_kanban_task_snapshot("fcar-board", "t_123")

    assert set(snapshot) == {
        "hostId",
        "boardSlug",
        "taskId",
        "revision",
        "status",
        "observedAt",
    }
    assert snapshot["boardSlug"] == "fcar-board"
    assert snapshot["taskId"] == "t_123"
    assert snapshot["status"] == "review"
    assert snapshot["revision"] == "2"
    assert snapshot["hostId"]
    assert snapshot["observedAt"].endswith("Z")
    after = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in db_path.parent.iterdir()
        if p.is_file()
    }
    assert after == before


def test_board_slug_with_underscore_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    _create_board(tmp_path, "fcar_board")
    snapshot = read_kanban_task_snapshot("fcar_board", "t_123")
    assert snapshot["boardSlug"] == "fcar_board"
    assert snapshot["status"] == "review"
    assert snapshot["revision"] == "2"


def test_missing_task_raises_task_not_found(tmp_path, monkeypatch):
    _seeded(tmp_path, monkeypatch)
    with pytest.raises(KanbanTaskNotFound):
        read_kanban_task_snapshot("fcar-board", "t_missing")


def test_missing_board_never_creates_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("unknown-board", "t_missing")
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("default", "t_missing")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("board", "task"),
    [
        ("../escape", "t_123"),
        ("fcar-board", "../escape"),
        ("fcar/board", "t_123"),
        ("fcar-board", "x" * 201),
        ("fcar-board\n", "t_123"),
        ("fcar-board", "t_123\n"),
    ],
)
def test_rejects_traversal_and_trailing_newline_ids(board, task, tmp_path, monkeypatch):
    _seeded(tmp_path, monkeypatch)
    with pytest.raises(InvalidKanbanIdentifier):
        read_kanban_task_snapshot(board, task)


def test_symlinked_db_is_rejected(tmp_path, monkeypatch):
    real_root = tmp_path / "real"
    real_db = _create_board(real_root, "fcar-board")
    home = tmp_path / "home"
    board_dir = home / "kanban" / "boards" / "fcar-board"
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(
        '{"slug":"fcar-board"}\n', encoding="utf-8"
    )
    (board_dir / "kanban.db").symlink_to(real_db)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("fcar-board", "t_123")


def test_symlinked_home_root_is_rejected(tmp_path, monkeypatch):
    real_root = tmp_path / "real"
    _create_board(real_root, "fcar-board")
    link_root = tmp_path / "linked-home"
    link_root.symlink_to(real_root)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(link_root))
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("fcar-board", "t_123")


def test_symlinked_home_ancestor_is_rejected(tmp_path, monkeypatch):
    real_base = tmp_path / "base"
    _create_board(real_base / "nested", "fcar-board")
    link_dir = tmp_path / "linkdir"
    link_dir.symlink_to(real_base)
    monkeypatch.setenv(
        "HERMES_KANBAN_HOME", str(link_dir / "nested")
    )
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("fcar-board", "t_123")


def test_symlinked_metadata_is_rejected(tmp_path, monkeypatch):
    real_root = tmp_path / "real"
    _create_board(real_root, "fcar-board")
    real_meta = real_root / "kanban" / "boards" / "fcar-board" / "board.json"
    home = tmp_path / "home"
    board_dir = home / "kanban" / "boards" / "fcar-board"
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").symlink_to(real_meta)
    import shutil

    shutil.copy(
        real_root / "kanban" / "boards" / "fcar-board" / "kanban.db",
        board_dir / "kanban.db",
    )
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("fcar-board", "t_123")


def test_oversized_metadata_is_rejected(tmp_path, monkeypatch):
    db_path = _seeded(tmp_path, monkeypatch)
    metadata = db_path.parent / "board.json"
    payload = '{"slug":"fcar-board","pad":"' + ("x" * (64 * 1024)) + '"}'
    metadata.write_text(payload, encoding="utf-8")
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("fcar-board", "t_123")


def test_metadata_slug_mismatch_is_rejected(tmp_path, monkeypatch):
    db_path = _seeded(tmp_path, monkeypatch)
    (db_path.parent / "board.json").write_text(
        '{"slug":"other-board"}\n', encoding="utf-8"
    )
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("fcar-board", "t_123")


def test_committed_wal_data_is_visible_and_consistent(tmp_path, monkeypatch):
    db_path = _seeded(tmp_path, monkeypatch)
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    # A single commit changes both status and latest event; the snapshot must
    # observe both from the same consistent read transaction.
    writer.execute("UPDATE tasks SET status = ? WHERE id = ?", ("done", "t_123"))
    writer.execute(
        "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)",
        ("t_123", "status", 3),
    )
    writer.commit()
    try:
        snapshot = read_kanban_task_snapshot("fcar-board", "t_123")
    finally:
        writer.close()
    assert snapshot["status"] == "done"
    assert snapshot["revision"] == "3"


def test_reader_cannot_write_or_initialize(tmp_path, monkeypatch):
    db_path = _seeded(tmp_path, monkeypatch)
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO tasks (id, status) VALUES ('t_new', 'open')"
            )
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "CREATE TABLE probe (id INTEGER PRIMARY KEY)"
            )
    finally:
        conn.close()
    # No stray files were initialized by the reader.
    assert not (db_path.parent / "probe").exists()
