from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope
from gateway.config import GatewayConfig, PlatformConfig
from gateway.kanban_snapshot import (
    InvalidKanbanIdentifier,
    KanbanBoardNotFound,
    KanbanTaskNotFound,
    read_kanban_task_snapshot,
)
from gateway.platforms.api_server import APIServerAdapter


def _create_board(root: Path, board: str, task_id: str = "t_123") -> Path:
    board_dir = root / "kanban" / "boards" / board
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text('{"slug":"fcar-board"}\n', encoding="utf-8")
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


def test_read_snapshot_is_strict_and_does_not_mutate_the_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = _create_board(tmp_path, "fcar-board")
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in db_path.parent.iterdir()
        if path.is_file()
    }

    snapshot = read_kanban_task_snapshot("fcar-board", "t_123")

    assert snapshot == {
        "hostId": snapshot["hostId"],
        "boardSlug": "fcar-board",
        "taskId": "t_123",
        "revision": "2",
        "status": "review",
        "observedAt": snapshot["observedAt"],
    }
    assert snapshot["hostId"]
    assert snapshot["observedAt"].endswith("Z")
    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in db_path.parent.iterdir()
        if path.is_file()
    }
    assert after == before


def test_missing_board_and_task_never_create_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    before = list(tmp_path.rglob("*"))

    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("unknown-board", "t_missing")
    with pytest.raises(KanbanBoardNotFound):
        read_kanban_task_snapshot("default", "t_missing")

    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("board", "task"),
    [
        ("../escape", "t_123"),
        ("fcar-board", "../escape"),
        ("fcar/board", "t_123"),
        ("fcar-board", "x" * 201),
    ],
)
def test_snapshot_rejects_traversal_and_unbounded_identifiers(board, task, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with pytest.raises(InvalidKanbanIdentifier):
        read_kanban_task_snapshot(board, task)


@pytest.mark.asyncio
async def test_profile_route_requires_the_selected_profiles_token(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    profiles = {
        "operator-a": root / "profiles" / "operator-a",
        "operator-b": root / "profiles" / "operator-b",
    }
    keys = {"operator-a": "a" * 32, "operator-b": "b" * 32}
    for profile, home in profiles.items():
        home.mkdir(parents=True)
        (home / ".env").write_text(f"API_SERVER_KEY={keys[profile]}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **_kwargs: list(profiles.items()))
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", profiles.__getitem__)
    _create_board(root, "fcar-board")

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = type("Runner", (), {"config": GatewayConfig(multiplex_profiles=True)})()
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, f"/p/{{profile}}{path}", handler)

    secret_scope.set_multiplex_active(True)
    try:
        async with TestClient(TestServer(app)) as client:
            rejected = await client.get(
                "/p/operator-a/v1/kanban/boards/fcar-board/tasks/t_123",
                headers={"Authorization": f"Bearer {keys['operator-b']}"},
            )
            accepted = await client.get(
                "/p/operator-a/v1/kanban/boards/fcar-board/tasks/t_123",
                headers={"Authorization": f"Bearer {keys['operator-a']}"},
            )
            accepted_payload = await accepted.json()
    finally:
        secret_scope.set_multiplex_active(False)

    assert rejected.status == 401
    assert accepted.status == 200
    assert accepted_payload == {
        "hostId": accepted_payload["hostId"],
        "boardSlug": "fcar-board",
        "taskId": "t_123",
        "revision": "2",
        "status": "review",
        "observedAt": accepted_payload["observedAt"],
    }


def test_missing_task_returns_404_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "unrelated.db"))
    _create_board(tmp_path, "fcar-board", task_id="t_present")
    before = list(tmp_path.rglob("*"))

    with pytest.raises(KanbanTaskNotFound):
        read_kanban_task_snapshot("fcar-board", "t_absent")

    assert list(tmp_path.rglob("*")) == before
    assert not (tmp_path / "unrelated.db").exists()


def test_capabilities_advertise_only_the_native_get_route():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    routes = {(method, path) for method, path, _handler in adapter._http_route_table()}
    assert ("GET", "/v1/kanban/boards/{board_slug}/tasks/{task_id}") in routes
    assert not any(path.startswith("/v1/kanban/") and method != "GET" for method, path in routes)
