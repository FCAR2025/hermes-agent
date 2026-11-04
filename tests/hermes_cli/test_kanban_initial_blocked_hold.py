"""Atomic non-dispatch semantics for tasks created initially blocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _event_count(conn, task_id: str, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()
    return int(row["n"])


def test_initially_blocked_task_survives_dispatch_ticks_without_spawn(
    kanban_home: Path,
) -> None:
    spawned: list[str] = []

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="staged research receipt",
            assignee="research",
            initial_status="blocked",
        )

        assert _event_count(conn, task_id, "blocked") == 1
        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
            result = kbd.dispatch_once(
                conn,
                spawn_fn=lambda task, *_args: spawned.append(task.id),
                reconcile_orphans=False,
            )
            assert result.promoted == 0
            assert result.spawned == []
            assert kb.get_task(conn, task_id).status == "blocked"

    assert spawned == []


def test_explicit_unblock_releases_initial_hold_once(
    kanban_home: Path,
    all_assignees_spawnable,
) -> None:
    spawned: list[str] = []

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="approved research receipt",
            assignee="research",
            initial_status="blocked",
        )

        assert kb.unblock_task(conn, task_id)
        assert not kb.unblock_task(conn, task_id)
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, *_args: spawned.append(task.id) or 4242,
            reconcile_orphans=False,
        )

        assert spawned == [task_id]
        assert [item[0] for item in result.spawned] == [task_id]
        assert kb.get_task(conn, task_id).status == "running"
        assert _event_count(conn, task_id, "unblocked") == 1


def test_idempotent_create_replay_does_not_restore_initial_hold(
    kanban_home: Path,
) -> None:
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="research receipt",
            assignee="research",
            initial_status="blocked",
            idempotency_key="research-receipt-1",
        )
        assert kb.unblock_task(conn, task_id)
        assert kb.claim_task(conn, task_id, claimer="supervised-worker") is not None

        replay_id = kb.create_task(
            conn,
            title="research receipt replay",
            assignee="research",
            initial_status="blocked",
            idempotency_key="research-receipt-1",
        )

        assert replay_id == task_id
        assert kb.get_task(conn, task_id).status == "running"
        assert _event_count(conn, task_id, "created") == 1
        assert _event_count(conn, task_id, "blocked") == 1


def test_existing_create_modes_do_not_gain_sticky_blocks(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        ready_id = kb.create_task(conn, title="ordinary", initial_status="running")
        triage_id = kb.create_task(
            conn,
            title="triage",
            initial_status="running",
            triage=True,
        )

        assert kb.get_task(conn, ready_id).status == "ready"
        assert kb.get_task(conn, triage_id).status == "triage"
        assert _event_count(conn, ready_id, "blocked") == 0
        assert _event_count(conn, triage_id, "blocked") == 0
