"""Completion-time evidence, live-claim, and blocked/unclaimed guards."""
from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_cli.kanban_db_connect import write_txn


class EmptyCompletionError(ValueError):
    """``complete_task`` refused: no substantive ``result``, ``summary``, or
    stored result. A ``ValueError`` so tool error handlers treat it as
    recoverable. Review approvals are exempt (the human is the record)."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"completion blocked: {task_id} has no result or summary evidence"
        )


class LiveClaimError(ValueError):
    """``complete_task`` refused: the task is ``running`` under a live claim and
    the caller neither owns its run (``expected_run_id``) nor passed ``force``.
    Completing anyway would close the worker's run row underneath a process
    that is still executing. A ``ValueError`` so tool error handlers treat it
    as recoverable."""

    def __init__(self, task_id: str):
        super().__init__(
            f"{task_id} is running under a live worker claim; pass expected_run_id "
            "(worker ownership) or force=True (explicit operator override) instead "
            "of closing the live run"
        )


def _claim_is_live(trow) -> bool:
    """True when a ``running`` task's claim still protects a run: the worker process
    it spawned exists (PID + start-time fingerprint). A claim whose worker is gone,
    or a library/CLI claim that never spawned one, has no run to protect. TTL expiry
    is deliberately not consulted: ``reclaim_stale_tasks`` extends, not reclaims, the
    claim of a live worker, so the process is the liveness authority here too."""
    from hermes_cli.kanban_db import _worker_alive

    return bool(
        trow["status"] == "running"
        and trow["claim_lock"] is not None
        and trow["worker_pid"]
        and _worker_alive(trow["worker_pid"], trow["worker_started_at"])
    )


def _is_blocked_unclaimed(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(
        """
        SELECT 1
          FROM tasks
         WHERE id = ?
           AND status = 'blocked'
           AND current_run_id IS NULL
           AND worker_pid IS NULL
           AND claim_lock IS NULL
           AND claim_expires IS NULL
        """,
        (task_id,),
    ).fetchone() is not None


def _substantive_text(value: Optional[str]) -> bool:
    return bool(value is not None and str(value).strip())


def _gate_empty_completion(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str],
    summary: Optional[str],
    audit_failure: bool = True,
) -> None:
    """Refuse a completion that would leave the card with no evidence.

    Review approvals are exempt: a human vouches for the card and
    ``_REVIEW_APPROVED_NOTE`` is the documented record. When
    ``audit_failure=False``, rejection stays read-only for strict CAS callers.
    """
    from hermes_cli.kanban_db import _append_event, _first_line

    row = conn.execute(
        "SELECT status, result FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    if row["status"] == "review":
        return
    stored = row["result"]
    if _substantive_text(result) or _substantive_text(summary) or _substantive_text(stored):
        return
    if audit_failure:
        with write_txn(conn):
            _append_event(
                conn, task_id, "completion_blocked_empty_result",
                {
                    "result_preview": _first_line(result, 200) or None,
                    "summary_preview": _first_line(summary, 200) or None,
                },
            )
    raise EmptyCompletionError(task_id)


def _gate_completion_evidence(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str],
    summary: Optional[str],
    require_blocked_unclaimed: bool,
    acceptance: object,
) -> bool:
    """Enforce empty-completion evidence unless PR acceptance covers a strict receipt."""
    if not require_blocked_unclaimed or acceptance is not None:
        return True
    if not _is_blocked_unclaimed(conn, task_id):
        return False
    _gate_empty_completion(
        conn, task_id, result=result, summary=summary, audit_failure=False,
    )
    return True


def _synthesized_run_handoff(
    *,
    prior_status: Optional[str],
    summary: Optional[str],
    result: Optional[str],
    handoff_summary: Optional[str],
    metadata: Optional[dict],
    acceptance: object,
    review_note: str,
) -> Optional[tuple[Optional[str], Optional[dict]]]:
    """Return ``(summary, metadata)`` when a never-claimed card needs a closing run."""
    if prior_status == "review" and not handoff_summary and not metadata:
        return review_note, {"source_status": "review", "approval": "manual"}
    if acceptance is not None and not metadata:
        pr_url = acceptance[1].get("pr_url")
        if pr_url:
            return handoff_summary, {"published_pr": pr_url}
    if summary or metadata or result or prior_status == "review":
        return handoff_summary, metadata
    return None
