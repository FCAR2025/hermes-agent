"""FCAR Action Gateway Telegram command lane tests.

These commands are intentionally separate from Hermes native /approve and /deny.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

KEY = "telegram-test:fcar-gateway:pending:001"


@pytest.fixture
def gateway_double(monkeypatch):
    """Supply observable unit doubles for the host-owned private gateway."""
    state = SimpleNamespace(pending=True)

    def queue_summary(_gateway_dir):
        pending = []
        if state.pending:
            pending.append(
                {
                    "idempotency_key": KEY,
                    "initiator": {"agent": "hermes"},
                    "action_class": "external_send",
                    "channel": "telegram",
                    "payload_hash": "sha256:test-payload",
                    "payload": {"message": "secret payload should not appear", "target": "dry-run-only"},
                }
            )
        return {
            "pending_approvals": pending,
            "pending_approval_count": len(pending),
            "active_intent_count": len(pending),
            "ledger_entry_count": 1,
            "status_counts": {"rejected": 0 if state.pending else 1},
        }

    def reject_dry_run(**_kwargs):
        state.pending = False
        return {"decision": "gateway_review_rejected"}

    contract = SimpleNamespace(
        gateway_status=MagicMock(
            return_value={"ledger_entry_count": 1, "status_counts_canonical_only": {"pending_approval": 1}}
        ),
        audit_ledger=MagicMock(return_value={"status": "AUDIT_PASS"}),
        compute_payload_hash=MagicMock(return_value="sha256:000000000000testhash000001"),
        approve_dry_run=MagicMock(return_value={"decision": "approval_simulated"}),
        execute_mock_readback=MagicMock(return_value={"decision": "mock_readback_verified"}),
    )
    review = SimpleNamespace(
        queue_summary=MagicMock(side_effect=queue_summary),
        reject_dry_run=MagicMock(side_effect=reject_dry_run),
    )

    from gateway import fcar_action_gateway_commands as commands

    monkeypatch.setattr(commands, "_load_gateway_modules", lambda: (contract, review))
    return SimpleNamespace(contract=contract, review=review, state=state)


def test_native_hermes_approve_and_deny_are_not_fcar_gateway_commands(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command, parse_fcar_gateway_command

    assert parse_fcar_gateway_command("/approve") is None
    assert parse_fcar_gateway_command("/deny") is None
    assert parse_fcar_gateway_command("approve") is None

    assert handle_fcar_gateway_command("/approve", gateway_dir=tmp_path).consumed is False
    assert handle_fcar_gateway_command("/deny", gateway_dir=tmp_path).consumed is False


def test_fcar_gateway_list_reads_pending_queue_without_payload_values(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = KEY
    result = handle_fcar_gateway_command("/fcar_gateway_list", gateway_dir=tmp_path)

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "Pending approval: 1" in result.text
    assert key in result.text
    assert "secret payload" not in result.text


def test_fcar_gateway_hyphen_alias_is_supported_but_not_required(tmp_path):
    from gateway.fcar_action_gateway_commands import parse_fcar_gateway_command

    parsed = parse_fcar_gateway_command("/fcar-gateway-list")

    assert parsed is not None
    assert parsed.command == "list"
    assert parsed.used_hyphen_alias is True


def test_fcar_gateway_reject_calls_private_review_contract(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = KEY
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_reject {key} duplicate candidate", gateway_dir=tmp_path, reviewer="telegram-test", allowed_reviewers={"telegram-test"}
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "Rejected" in result.text
    assert "Hermes native" not in result.text
    gateway_double.review.reject_dry_run.assert_called_once_with(
        gateway_dir=tmp_path,
        idempotency_key=key,
        reviewer="telegram-test",
        rejection_reason="duplicate candidate",
    )
    summary = gateway_double.review.queue_summary(tmp_path)
    assert summary["pending_approval_count"] == 0
    assert summary["status_counts"]["rejected"] == 1


def test_fcar_gateway_approve_runs_dry_run_mock_readback_chain(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = KEY
    result = handle_fcar_gateway_command(f"/fcar_gateway_approve {key}", gateway_dir=tmp_path, reviewer="telegram-test", allowed_reviewers={"telegram-test"})

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "Dry-run approved" in result.text
    assert "mock readback verified" in result.text.lower()
    gateway_double.contract.compute_payload_hash.assert_called_once_with({"key": key})
    gateway_double.contract.approve_dry_run.assert_called_once_with(
        gateway_dir=tmp_path,
        idempotency_key=key,
        approval_id="fcar-gateway-dry-run:telegram-test:sthash000001",
        allowed_executor="fcar-gateway.mock-executor",
        readback_probe={
            "type": "mock_fcar_gateway_readback",
            "idempotency_key": key,
            "reviewer": "telegram-test",
            "dry_run_only": True,
        },
    )
    gateway_double.contract.execute_mock_readback.assert_called_once_with(
        gateway_dir=tmp_path,
        idempotency_key=key,
        mock_readback={
            "type": "mock_fcar_gateway_readback",
            "idempotency_key": key,
            "verified_by": "telegram-test",
            "dry_run_only": True,
            "external_side_effect": False,
        },
    )


def test_fcar_gateway_status_reports_audit_pass(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    result = handle_fcar_gateway_command("/fcar_gateway_status", gateway_dir=tmp_path)

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "AUDIT_PASS" in result.text
    assert "dry-run only" in result.text.lower()
    gateway_double.contract.gateway_status.assert_called_once_with(tmp_path)
    gateway_double.contract.audit_ledger.assert_called_once_with(tmp_path)


def test_fcar_gateway_approve_requires_fcar_operator_allowlist(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = KEY
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_approve {key}",
        gateway_dir=tmp_path,
        reviewer="telegram:intruder",
        allowed_reviewers={"telegram:carl"},
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "not authorized" in result.text.lower()
    gateway_double.contract.approve_dry_run.assert_not_called()


def test_fcar_gateway_reject_requires_fcar_operator_allowlist(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = KEY
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_reject {key} bad candidate",
        gateway_dir=tmp_path,
        reviewer="telegram:intruder",
        allowed_reviewers={"telegram:carl"},
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "not authorized" in result.text.lower()
    gateway_double.review.reject_dry_run.assert_not_called()


def test_fcar_gateway_allowed_operator_can_approve_dry_run(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = KEY
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_approve {key}",
        gateway_dir=tmp_path,
        reviewer="telegram:carl",
        allowed_reviewers={"telegram:carl"},
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "Dry-run approved" in result.text
    gateway_double.contract.execute_mock_readback.assert_called_once()


def test_fcar_gateway_pending_digest_summarizes_without_payload_leak(tmp_path, gateway_double):
    from gateway.fcar_action_gateway_commands import build_pending_digest, handle_fcar_gateway_command

    digest = build_pending_digest(tmp_path)

    assert digest.pending_count == 1
    assert "1 FCAR gateway action needs review" in digest.text
    assert KEY in digest.text
    assert "secret payload" not in digest.text

    command_result = handle_fcar_gateway_command("/fcar_gateway_digest", gateway_dir=tmp_path)
    assert command_result.consumed is True
    assert command_result.external_side_effect is False
    assert "1 FCAR gateway action needs review" in command_result.text
