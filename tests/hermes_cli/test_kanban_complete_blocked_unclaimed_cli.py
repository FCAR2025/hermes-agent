"""CLI regressions for terminal receipts on unused blocked tasks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _create_blocked(home: Path, *, completion_contract: str | None = None) -> str:
    args = [
        "kanban",
        "create",
        "terminal receipt candidate",
        "--initial-status",
        "blocked",
        "--json",
    ]
    if completion_contract is not None:
        args.extend(("--completion-contract", completion_contract))
    created = _run_hermes(home, *args)
    assert created.returncode == 0, created.stderr
    return str(json.loads(created.stdout)["id"])


def test_complete_if_blocked_unclaimed_rejects_intervening_claim_without_mutation(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    task_id = _create_blocked(home, completion_contract="acme/repo")

    with kbc.connect_closing(home / "kanban.db") as conn:
        pre_read = kb.get_task(conn, task_id)
        assert pre_read is not None and pre_read.status == "blocked"

    unblocked = _run_hermes(home, "kanban", "unblock", task_id)
    assert unblocked.returncode == 0, unblocked.stderr
    claimed = _run_hermes(home, "kanban", "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr

    with kbc.connect_closing(home / "kanban.db") as conn:
        conn.execute("UPDATE tasks SET worker_pid = 4242 WHERE id = ?", (task_id,))
        before_task = kb.get_task(conn, task_id)
        assert before_task is not None
        assert before_task.current_run_id is not None
        assert before_task.worker_pid == 4242
        assert before_task.claim_lock is not None
        assert before_task.claim_expires is not None
        assert before_task.completion_contract == "acme/repo"
        before_events = kb.list_events(conn, task_id)
        before_runs = kb.list_runs(conn, task_id)

    denied = _run_hermes(
        home,
        "kanban",
        "complete",
        task_id,
        "--if-blocked-unclaimed",
        "--result",
        "must not land",
        "--summary",
        "stale terminal receipt",
        "--metadata",
        '{"receipt": "stale", "published_pr": "https://github.com/acme/repo/pull/7"}',
    )

    assert denied.returncode == 1
    assert "blocked and unclaimed" in denied.stderr
    with kbc.connect_closing(home / "kanban.db") as conn:
        after_task = kb.get_task(conn, task_id)
        assert after_task == before_task
        assert kb.list_events(conn, task_id) == before_events
        assert kb.list_runs(conn, task_id) == before_runs


def test_complete_if_blocked_unclaimed_records_unused_task_receipt(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    task_id = _create_blocked(home)

    completed = _run_hermes(
        home,
        "kanban",
        "complete",
        task_id,
        "--if-blocked-unclaimed",
        "--result",
        "terminal receipt stored",
        "--summary",
        "CI allocation ended without a worker claim",
        "--metadata",
        '{"receipt": "terminal", "outcome": "held"}',
    )

    assert completed.returncode == 0, completed.stderr
    assert f"Completed {task_id}" in completed.stdout
    with kbc.connect_closing(home / "kanban.db") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"
        assert task.result == "terminal receipt stored"
        assert task.completed_at is not None
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task.claim_expires is None
        assert task.worker_pid is None
        runs = kb.list_runs(conn, task_id)
        assert len(runs) == 1
        assert runs[0].outcome == "completed"
        assert runs[0].summary == "CI allocation ended without a worker claim"
        assert runs[0].metadata == {"receipt": "terminal", "outcome": "held"}
        assert [event.kind for event in kb.list_events(conn, task_id)].count("completed") == 1


def test_complete_if_blocked_unclaimed_rejects_empty_without_pr_contract_or_mutation(tmp_path):
    import pytest

    home = tmp_path / "hermes"
    home.mkdir()
    none_contract_id = _create_blocked(home)
    local_contract_id = _create_blocked(home, completion_contract="local-only")

    with kbc.connect_closing(home / "kanban.db") as conn:
        before_none_events = kb.list_events(conn, none_contract_id)
        before_local_events = kb.list_events(conn, local_contract_id)

        with pytest.raises(kb.EmptyCompletionError):
            kb.complete_task(conn, none_contract_id, require_blocked_unclaimed=True)
        with pytest.raises(kb.EmptyCompletionError):
            kb.complete_task(
                conn,
                local_contract_id,
                result="   ",
                summary="",
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"},
                require_blocked_unclaimed=True,
            )

        for tid, before_events in (
            (none_contract_id, before_none_events),
            (local_contract_id, before_local_events),
        ):
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "blocked"
            assert task.result is None
            assert kb.list_runs(conn, tid) == []
            assert kb.list_events(conn, tid) == before_events

    cli_denied = _run_hermes(home, "kanban", "complete", none_contract_id, "--if-blocked-unclaimed")
    assert cli_denied.returncode == 1
    assert "no result or summary evidence" in cli_denied.stderr
    with kbc.connect_closing(home / "kanban.db") as conn:
        after_task = kb.get_task(conn, none_contract_id)
        assert after_task is not None and after_task.status == "blocked"
        assert kb.list_events(conn, none_contract_id) == before_none_events
