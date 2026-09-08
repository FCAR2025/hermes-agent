"""FCAR Action Gateway namespaced command handling for Hermes platforms.

This module is deliberately separate from Hermes native approval handling.
It never resolves Hermes /approve or /deny and never executes live side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import shlex
import sys
from typing import Iterable, Literal

SCRIPTS_DIR = Path("/home/info/scripts")
DEFAULT_GATEWAY_DIR = Path("/home/info/.omx/action-gateway")


def _load_gateway_modules():
    """Load the host-owned FCAR gateway implementation when it is installed."""
    if SCRIPTS_DIR.is_dir() and str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    contract = importlib.import_module("fcar_action_gateway_contract")
    review = importlib.import_module("fcar_action_gateway_review")
    return contract, review


CommandName = Literal["list", "status", "digest", "approve", "reject", "mock_readback"]

COMMAND_ALIASES: dict[str, CommandName] = {
    "/fcar_gateway_list": "list",
    "/fcar-gateway-list": "list",
    "/fcar_gateway_status": "status",
    "/fcar-gateway-status": "status",
    "/fcar_gateway_digest": "digest",
    "/fcar-gateway-digest": "digest",
    "/fcar_gateway_approve": "approve",
    "/fcar-gateway-approve": "approve",
    "/fcar_gateway_reject": "reject",
    "/fcar-gateway-reject": "reject",
    "/fcar_gateway_mock_readback": "mock_readback",
    "/fcar-gateway-mock-readback": "mock_readback",
}

PRIVILEGED_COMMANDS = {"approve", "reject", "mock_readback"}
HERMES_NATIVE_APPROVAL_COMMANDS = {"/approve", "/deny", "/reject"}


@dataclass(frozen=True)
class FCARGatewayCommand:
    command: CommandName
    args: tuple[str, ...]
    raw_command: str
    used_hyphen_alias: bool = False


@dataclass(frozen=True)
class FCARGatewayCommandResult:
    consumed: bool
    text: str = ""
    external_side_effect: bool = False
    dry_run_only: bool = True


@dataclass(frozen=True)
class FCARGatewayDigest:
    pending_count: int
    text: str
    external_side_effect: bool = False
    dry_run_only: bool = True


def _normalize_command_token(token: str) -> str:
    """Return lowercase Telegram command without bot suffix."""
    return token.strip().split("@", 1)[0].lower()


def parse_fcar_gateway_command(text: str | None) -> FCARGatewayCommand | None:
    """Parse FCAR gateway commands and ignore Hermes native approvals."""
    if not text or not text.strip().startswith("/"):
        return None
    try:
        parts = shlex.split(text.strip())
    except ValueError:
        # Fall back so malformed quotes still stay inside FCAR lane if the
        # command prefix is FCAR namespaced.
        parts = text.strip().split()
    if not parts:
        return None

    token = _normalize_command_token(parts[0])
    if token in HERMES_NATIVE_APPROVAL_COMMANDS:
        return None

    command = COMMAND_ALIASES.get(token)
    if command is None:
        return None
    return FCARGatewayCommand(
        command=command,
        args=tuple(parts[1:]),
        raw_command=token,
        used_hyphen_alias="-" in token,
    )


def _usage() -> str:
    return (
        "FCAR gateway commands are separate from Hermes /approve and /deny.\n"
        "Use Telegram-safe underscore commands:\n"
        "• /fcar_gateway_list\n"
        "• /fcar_gateway_status\n"
        "• /fcar_gateway_digest\n"
        "• /fcar_gateway_approve <idempotency_key>\n"
        "• /fcar_gateway_reject <idempotency_key> <reason>\n"
        "• /fcar_gateway_mock_readback <idempotency_key>"
    )


def _split_allowed_reviewers(value: str | Iterable[str] | None) -> set[str]:
    if value is None:
        value = os.getenv("FCAR_ACTION_GATEWAY_OPERATORS", "")
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("\n", ",").split(",")
        return {item.strip() for item in raw if item.strip()}
    return {str(item).strip() for item in value if str(item).strip()}


def _reviewer_allowed(reviewer: str, allowed_reviewers: str | Iterable[str] | None) -> bool:
    allowed = _split_allowed_reviewers(allowed_reviewers)
    if not allowed:
        return False
    if "*" in allowed or reviewer in allowed:
        return True
    if ":" in reviewer:
        bare = reviewer.split(":", 1)[1]
        if bare in allowed:
            return True
    return False


def _unauthorized_text(reviewer: str) -> str:
    return (
        f"FCAR gateway reviewer not authorized: {reviewer}.\n"
        "List/status/digest are safe, but approve/reject/mock readback require "
        "FCAR_ACTION_GATEWAY_OPERATORS or config fcar_action_gateway_operators.\n"
        "Hermes native /approve and /deny are unaffected."
    )


def _queue_text(gateway_dir: Path) -> str:
    _, gateway_review = _load_gateway_modules()
    summary = gateway_review.queue_summary(gateway_dir)
    pending = summary["pending_approvals"]
    lines = [
        "FCAR Action Gateway review queue (dry-run only)",
        f"Pending approval: {summary['pending_approval_count']}",
        f"Active intents: {summary['active_intent_count']}",
        f"Ledger entries: {summary['ledger_entry_count']}",
        "",
        "Hermes native /approve and /deny are not used for this queue.",
    ]
    if pending:
        lines.append("")
        for index, row in enumerate(pending, 1):
            initiator = row.get("initiator") or {}
            lines.append(
                f"{index}. {row['idempotency_key']} | {initiator.get('agent')} | "
                f"{row.get('action_class')}:{row.get('channel')} | {row.get('payload_hash')}"
            )
    return "\n".join(lines)


def build_pending_digest(gateway_dir: Path | str) -> FCARGatewayDigest:
    _, gateway_review = _load_gateway_modules()
    path = Path(gateway_dir)
    summary = gateway_review.queue_summary(path)
    pending = summary["pending_approvals"]
    count = int(summary["pending_approval_count"])
    plural = "action" if count == 1 else "actions"
    verb = "needs" if count == 1 else "need"
    lines = [
        f"{count} FCAR gateway {plural} {verb} review",
        "Dry-run only. No live executor is enabled.",
        "Use /fcar_gateway_list to inspect and /fcar_gateway_approve or /fcar_gateway_reject only as an authorized FCAR operator.",
    ]
    if pending:
        lines.append("")
        for index, row in enumerate(pending[:10], 1):
            initiator = row.get("initiator") or {}
            lines.append(
                f"{index}. {row['idempotency_key']} | {initiator.get('agent')} | "
                f"{row.get('action_class')}:{row.get('channel')} | {row.get('payload_hash')}"
            )
    return FCARGatewayDigest(pending_count=count, text="\n".join(lines))


def _status_text(gateway_dir: Path) -> str:
    gateway_contract, _ = _load_gateway_modules()
    status = gateway_contract.gateway_status(gateway_dir)
    audit = gateway_contract.audit_ledger(gateway_dir)
    return (
        "FCAR Action Gateway status (dry-run only)\n"
        f"Audit: {audit['status']}\n"
        f"Ledger entries: {status['ledger_entry_count']}\n"
        f"Canonical status counts: {status['status_counts_canonical_only']}\n"
        "External side effect: false"
    )


def _approval_id(*, reviewer: str, key: str) -> str:
    gateway_contract, _ = _load_gateway_modules()
    safe_reviewer = "".join(ch for ch in reviewer if ch.isalnum() or ch in {"-", "_"}) or "operator"
    return f"fcar-gateway-dry-run:{safe_reviewer}:{gateway_contract.compute_payload_hash({'key': key})[-12:]}"


def _approve_then_mock(gateway_dir: Path, key: str, reviewer: str) -> tuple[dict, dict]:
    gateway_contract, _ = _load_gateway_modules()
    readback_probe = {
        "type": "mock_fcar_gateway_readback",
        "idempotency_key": key,
        "reviewer": reviewer,
        "dry_run_only": True,
    }
    approved = gateway_contract.approve_dry_run(
        gateway_dir=gateway_dir,
        idempotency_key=key,
        approval_id=_approval_id(reviewer=reviewer, key=key),
        allowed_executor="fcar-gateway.mock-executor",
        readback_probe=readback_probe,
    )
    if approved.get("decision") != "approval_simulated":
        return approved, {}
    readback = gateway_contract.execute_mock_readback(
        gateway_dir=gateway_dir,
        idempotency_key=key,
        mock_readback={
            "type": "mock_fcar_gateway_readback",
            "idempotency_key": key,
            "verified_by": reviewer,
            "dry_run_only": True,
            "external_side_effect": False,
        },
    )
    return approved, readback


def handle_fcar_gateway_command(
    text: str | None,
    *,
    gateway_dir: Path | str = DEFAULT_GATEWAY_DIR,
    reviewer: str = "telegram-operator",
    allowed_reviewers: str | Iterable[str] | None = None,
) -> FCARGatewayCommandResult:
    """Handle a namespaced FCAR gateway command if present.

    Returns consumed=False for every non-FCAR command, including Hermes native
    /approve and /deny.
    """
    parsed = parse_fcar_gateway_command(text)
    if parsed is None:
        return FCARGatewayCommandResult(consumed=False)

    path = Path(gateway_dir)
    try:
        if parsed.command == "list":
            return FCARGatewayCommandResult(consumed=True, text=_queue_text(path))

        if parsed.command == "status":
            return FCARGatewayCommandResult(consumed=True, text=_status_text(path))

        if parsed.command == "digest":
            return FCARGatewayCommandResult(consumed=True, text=build_pending_digest(path).text)

        if parsed.command in PRIVILEGED_COMMANDS and not _reviewer_allowed(reviewer, allowed_reviewers):
            return FCARGatewayCommandResult(consumed=True, text=_unauthorized_text(reviewer))

        if parsed.command == "reject":
            _, gateway_review = _load_gateway_modules()
            if len(parsed.args) < 2:
                return FCARGatewayCommandResult(consumed=True, text="Missing key or reason.\n" + _usage())
            key = parsed.args[0]
            reason = " ".join(parsed.args[1:]).strip()
            rejected = gateway_review.reject_dry_run(
                gateway_dir=path,
                idempotency_key=key,
                reviewer=reviewer,
                rejection_reason=reason,
            )
            if rejected.get("decision") != "gateway_review_rejected":
                return FCARGatewayCommandResult(
                    consumed=True,
                    text=f"FCAR gateway reject blocked: {rejected.get('reason') or rejected.get('decision')}",
                )
            return FCARGatewayCommandResult(
                consumed=True,
                text=f"Rejected FCAR gateway dry-run intent: {key}\nExternal side effect: false",
            )

        if parsed.command in {"approve", "mock_readback"}:
            if not parsed.args:
                return FCARGatewayCommandResult(consumed=True, text="Missing idempotency key.\n" + _usage())
            key = parsed.args[0]
            approved, readback = _approve_then_mock(path, key, reviewer)
            if approved.get("decision") != "approval_simulated":
                return FCARGatewayCommandResult(
                    consumed=True,
                    text=f"FCAR gateway approve blocked: {approved.get('reason') or approved.get('decision')}",
                )
            if readback.get("decision") != "mock_readback_verified":
                return FCARGatewayCommandResult(
                    consumed=True,
                    text=f"Dry-run approved but mock readback blocked: {readback.get('reason') or readback.get('decision')}",
                )
            return FCARGatewayCommandResult(
                consumed=True,
                text=(
                    f"Dry-run approved FCAR gateway intent: {key}\n"
                    "Mock readback verified.\n"
                    "External side effect: false"
                ),
            )
    except Exception as exc:  # fail closed into FCAR lane, not Hermes native approval
        return FCARGatewayCommandResult(
            consumed=True,
            text=f"FCAR gateway command failed closed: {type(exc).__name__}: {exc}",
        )

    return FCARGatewayCommandResult(consumed=True, text=_usage())
