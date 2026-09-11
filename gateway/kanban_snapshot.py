"""Read-only, authenticated Kanban task snapshot helper.

Serves the closed ``GET /v1/kanban/boards/{board_slug}/tasks/{task_id}``
document: ``{hostId, boardSlug, taskId, revision, status, observedAt}``.

Design constraints:

- The Kanban board is HOST-LOCAL and shared across profiles by design; the
  canonical ``kanban_home()`` anchors all paths. ``HERMES_KANBAN_DB`` is
  deliberately NOT honoured here.
- Strictly read-only: the SQLite file is opened with ``mode=ro`` and the
  connection is pinned with an explicit ``PRAGMA query_only = ON`` (the
  ``query_only=1`` URI parameter is not a PRAGMA and is not relied upon).
  ``immutable=1`` is never used so committed WAL frames stay visible.
  Nothing is ever created, initialized, migrated, or written.
- Status and event-revision reads happen inside ONE consistent SQLite read
  transaction (``BEGIN``) so a concurrent WAL writer cannot mix an old
  status with a new revision.
- Only ``tasks.id``/``tasks.status`` and ``MAX(task_events.id)`` are read —
  no titles, bodies, comments, outputs, or credentials.
- Lexically bounded identifiers (fullmatch, no traversal or trailing
  newline); every path component is checked against symlinks, including
  the root; board metadata is a regular file of at most 64 KiB.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")
_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_STATUS_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REVISION_RE = re.compile(r"[0-9]{1,32}")
_METADATA_LIMIT = 64 * 1024
_DEFAULT_BOARD = "default"


class KanbanSnapshotError(Exception):
    """Base error carrying a bounded HTTP status (no filesystem paths)."""

    http_status = 500


class InvalidKanbanIdentifier(KanbanSnapshotError):
    http_status = 400


class KanbanBoardNotFound(KanbanSnapshotError):
    http_status = 404


class KanbanTaskNotFound(KanbanSnapshotError):
    http_status = 404


def _kanban_home() -> Path:
    """Canonical shared kanban root (HERMES_KANBAN_HOME / default root)."""
    from hermes_cli.kanban_db import kanban_home

    home = Path(kanban_home())
    if not home.is_absolute():
        raise KanbanSnapshotError("Kanban home must be absolute")
    # Walk the ORIGINAL (unresolved) components so a symlinked root or any
    # symlinked ancestor is detectable; resolving first would hide them.
    current = Path(home.anchor)
    for part in home.parts[1:]:
        if part == "..":
            raise KanbanSnapshotError("Kanban home must not traverse parents")
        current = current / part
        if current.is_symlink():
            raise KanbanBoardNotFound("Kanban board not found")
    if not home.is_dir():
        raise KanbanBoardNotFound("Kanban board not found")
    # Safe: no component is a symlink, so resolve() does not change identity.
    return home.resolve()


def _reject_symlinks(root: Path, candidate: Path) -> None:
    """Refuse any symlinked component of ``candidate`` including ``root``."""
    if root.is_symlink():
        raise KanbanBoardNotFound("Kanban board not found")
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise KanbanBoardNotFound("Kanban board not found")


def _validate_identifiers(board_slug: str, task_id: str) -> None:
    if not isinstance(board_slug, str) or _BOARD_SLUG_RE.fullmatch(board_slug) is None:
        raise InvalidKanbanIdentifier("Invalid Kanban board slug")
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise InvalidKanbanIdentifier("Invalid Kanban task id")


def _read_metadata(metadata: Path, board_slug: str) -> None:
    """Read bounded board metadata and require an exact slug match."""
    try:
        if metadata.is_symlink() or not metadata.is_file():
            raise KanbanBoardNotFound("Kanban board not found")
        with metadata.open("rb") as handle:
            raw = handle.read(_METADATA_LIMIT + 1)
    except OSError:
        raise KanbanBoardNotFound("Kanban board not found")
    if len(raw) > _METADATA_LIMIT:
        raise KanbanBoardNotFound("Kanban board not found")
    try:
        meta: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise KanbanBoardNotFound("Kanban board not found")
    if not isinstance(meta, dict) or meta.get("slug") != board_slug:
        raise KanbanBoardNotFound("Kanban board not found")


def _resolve_board_db(home: Path, board_slug: str) -> Path:
    """Resolve the canonical DB path for ``board_slug``; 404 if absent."""
    if board_slug == _DEFAULT_BOARD:
        db_path = home / "kanban.db"
        _reject_symlinks(home, db_path)
        if not db_path.is_file():
            raise KanbanBoardNotFound("Kanban board not found")
        return db_path

    board_dir = home / "kanban" / "boards" / board_slug
    _reject_symlinks(home, board_dir)
    metadata = board_dir / "board.json"
    db_path = board_dir / "kanban.db"
    _reject_symlinks(home, metadata)
    _reject_symlinks(home, db_path)
    if not db_path.is_file():
        raise KanbanBoardNotFound("Kanban board not found")
    _read_metadata(metadata, board_slug)
    return db_path


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _bounded(value: Any, pattern: re.Pattern[str], error: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise KanbanSnapshotError(error)
    return value


def read_kanban_task_snapshot(board_slug: str, task_id: str) -> Dict[str, Optional[str]]:
    """Return the closed read-only snapshot for one Kanban task."""
    _validate_identifiers(board_slug, task_id)
    home = _kanban_home()
    db_path = _resolve_board_db(home, board_slug)

    # path.as_uri() yields a properly quoted file:// URI; mode=ro only.
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = ON")
        # One consistent read transaction: a concurrent WAL writer cannot
        # split an old status from a newly committed revision.
        conn.execute("BEGIN")
        try:
            if not _table_exists(conn, "tasks"):
                raise KanbanBoardNotFound("Kanban board not found")
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KanbanTaskNotFound("Kanban task not found")
            status = _bounded(row[0], _STATUS_RE, "Invalid Kanban task status")
            revision: Optional[str] = None
            if _table_exists(conn, "task_events"):
                rev_row = conn.execute(
                    "SELECT MAX(id) FROM task_events WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if rev_row and rev_row[0] is not None:
                    revision = _bounded(
                        str(int(rev_row[0])),
                        _REVISION_RE,
                        "Invalid Kanban task revision",
                    )
        finally:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        raise KanbanBoardNotFound("Kanban board not found")
    finally:
        conn.close()

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "hostId": socket.gethostname(),
        "boardSlug": board_slug,
        "taskId": task_id,
        "revision": revision,
        "status": status,
        "observedAt": observed_at,
    }
