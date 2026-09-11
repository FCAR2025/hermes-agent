"""FCAR Action Gateway Telegram command lane tests.

These commands are intentionally separate from Hermes native /approve and /deny.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_SCRIPTS = "/home/info/scripts"
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import fcar_action_gateway_contract as gateway_contract  # noqa: E402


KEY = "telegram-test:fcar-gateway:pending:001"


def _seed_gateway(gateway_dir: Path) -> str:
    gateway_contract.write_manifest(gateway_dir)
    intent = gateway_contract.sample_intent(
        intent_id="intent_telegram_test_001",
        agent="hermes",
        surface="telegram-test",
        action_class="external_send",
        channel="telegram",
        payload={"message": "secret payload should not appear", "target": "dry-run-only"},
        idempotency_key=KEY,
        session_id="telegram-test-session",
    )
    result = gateway_contract.ingest_intents([intent], gateway_dir)
    assert result["counts"]["canonical"] == 1
    return KEY


def _ledger_types(gateway_dir: Path) -> list[str]:
    return [
        json.loads(line)["entry_type"]
        for line in (gateway_dir / "ledger.jsonl").read_text().splitlines()
    ]


def test_native_hermes_approve_and_deny_are_not_fcar_gateway_commands(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command, parse_fcar_gateway_command

    assert parse_fcar_gateway_command("/approve") is None
    assert parse_fcar_gateway_command("/deny") is None
    assert parse_fcar_gateway_command("approve") is None

    assert handle_fcar_gateway_command("/approve", gateway_dir=tmp_path).consumed is False
    assert handle_fcar_gateway_command("/deny", gateway_dir=tmp_path).consumed is False


def test_fcar_gateway_list_reads_pending_queue_without_payload_values(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = _seed_gateway(tmp_path)
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


def test_fcar_gateway_reject_appends_gateway_review_entry_only(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command
    import fcar_action_gateway_review as gateway_review

    key = _seed_gateway(tmp_path)
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_reject {key} duplicate candidate", gateway_dir=tmp_path, reviewer="telegram-test", allowed_reviewers={"telegram-test"}
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "Rejected" in result.text
    assert "Hermes native" not in result.text
    assert "gateway_review_rejected" in _ledger_types(tmp_path)
    summary = gateway_review.queue_summary(tmp_path)
    assert summary["pending_approval_count"] == 0
    assert summary["status_counts"]["rejected"] == 1


def test_fcar_gateway_approve_runs_dry_run_mock_readback_chain(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = _seed_gateway(tmp_path)
    result = handle_fcar_gateway_command(f"/fcar_gateway_approve {key}", gateway_dir=tmp_path, reviewer="telegram-test", allowed_reviewers={"telegram-test"})

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "Dry-run approved" in result.text
    assert "mock readback verified" in result.text.lower()
    assert "approval_simulated" in _ledger_types(tmp_path)
    assert "mock_readback_verified" in _ledger_types(tmp_path)
    audit = gateway_contract.audit_ledger(tmp_path)
    assert audit["status"] == "AUDIT_PASS"


def test_fcar_gateway_status_reports_audit_pass(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    _seed_gateway(tmp_path)
    result = handle_fcar_gateway_command("/fcar_gateway_status", gateway_dir=tmp_path)

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "AUDIT_PASS" in result.text
    assert "dry-run only" in result.text.lower()


def test_fcar_gateway_approve_requires_fcar_operator_allowlist(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = _seed_gateway(tmp_path)
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_approve {key}",
        gateway_dir=tmp_path,
        reviewer="telegram:intruder",
        allowed_reviewers={"telegram:carl"},
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "not authorized" in result.text.lower()
    assert "approval_simulated" not in _ledger_types(tmp_path)


def test_fcar_gateway_reject_requires_fcar_operator_allowlist(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = _seed_gateway(tmp_path)
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_reject {key} bad candidate",
        gateway_dir=tmp_path,
        reviewer="telegram:intruder",
        allowed_reviewers={"telegram:carl"},
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "not authorized" in result.text.lower()
    assert "gateway_review_rejected" not in _ledger_types(tmp_path)


def test_fcar_gateway_allowed_operator_can_approve_dry_run(tmp_path):
    from gateway.fcar_action_gateway_commands import handle_fcar_gateway_command

    key = _seed_gateway(tmp_path)
    result = handle_fcar_gateway_command(
        f"/fcar_gateway_approve {key}",
        gateway_dir=tmp_path,
        reviewer="telegram:carl",
        allowed_reviewers={"telegram:carl"},
    )

    assert result.consumed is True
    assert result.external_side_effect is False
    assert "Dry-run approved" in result.text
    assert "mock_readback_verified" in _ledger_types(tmp_path)


def test_fcar_gateway_pending_digest_summarizes_without_payload_leak(tmp_path):
    from gateway.fcar_action_gateway_commands import build_pending_digest, handle_fcar_gateway_command

    _seed_gateway(tmp_path)
    digest = build_pending_digest(tmp_path)

    assert digest.pending_count == 1
    assert "1 FCAR gateway action needs review" in digest.text
    assert KEY in digest.text
    assert "secret payload" not in digest.text

    command_result = handle_fcar_gateway_command("/fcar_gateway_digest", gateway_dir=tmp_path)
    assert command_result.consumed is True
    assert command_result.external_side_effect is False
    assert "1 FCAR gateway action needs review" in command_result.text
