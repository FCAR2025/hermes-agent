"""Tests for FCAR Action Gateway ops helper scripts."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
_SCRIPTS = "/home/info/scripts"
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import fcar_action_gateway_contract as gateway_contract  # noqa: E402

KEY = "ops-script-test:fcar-gateway:pending:001"


def _seed_gateway(gateway_dir: Path) -> str:
    gateway_contract.write_manifest(gateway_dir)
    intent = gateway_contract.sample_intent(
        intent_id="intent_ops_script_test_001",
        agent="hermes",
        surface="ops-script-test",
        action_class="external_send",
        channel="telegram",
        payload={"message": "payload secret must not leak", "target": "dry-run"},
        idempotency_key=KEY,
    )
    result = gateway_contract.ingest_intents([intent], gateway_dir)
    assert result["counts"]["canonical"] == 1
    return KEY


def test_pending_digest_alert_omits_payload_and_is_disabled_by_default(tmp_path):
    from fcar_action_gateway_digest_alert import build_pending_alert, send_or_dry_run_alert

    _seed_gateway(tmp_path)
    alert = build_pending_alert(tmp_path)

    assert alert.pending_count == 1
    assert "1 FCAR gateway action needs review" in alert.text
    assert KEY in alert.text
    assert "payload secret" not in alert.text

    result = send_or_dry_run_alert(alert, send=True, token="token", chat_id="123", alerts_enabled=False)
    assert result["status"] == "ALERTS_DISABLED"
    assert result["external_side_effect"] is False


def test_pending_digest_alert_dry_run_never_sends(tmp_path):
    from fcar_action_gateway_digest_alert import build_pending_alert, send_or_dry_run_alert

    _seed_gateway(tmp_path)
    alert = build_pending_alert(tmp_path)
    result = send_or_dry_run_alert(alert, send=False, token="token", chat_id="123", alerts_enabled=True)

    assert result["status"] == "DRY_RUN"
    assert result["external_side_effect"] is False
    assert result["pending_count"] == 1


def test_disabled_executor_refuses_live_action(tmp_path):
    from fcar_action_gateway_executor_adapters import execute_internal_telegram_alert_disabled

    _seed_gateway(tmp_path)
    result = execute_internal_telegram_alert_disabled(
        gateway_dir=tmp_path,
        idempotency_key=KEY,
        token=None,
        token_secret=None,
        chat_id="123",
    )
    assert result["status"] == "TOKEN_REQUIRED"
    assert result["external_side_effect"] is False
    assert "executor token" in result["reason"]


def test_dashboard_renders_status_without_payload_values(tmp_path):
    from fcar_action_gateway_dashboard import render_dashboard_html

    _seed_gateway(tmp_path)
    html = render_dashboard_html(tmp_path)

    assert "FCAR Action Gateway Dashboard" in html
    assert KEY in html
    assert "needs_approval" in html
    assert "payload secret" not in html


def _seed_approved_ready_gateway(gateway_dir: Path) -> str:
    key = "ops-script-test:fcar-gateway:approved-ready:001"
    gateway_contract.write_manifest(gateway_dir)
    intent = gateway_contract.sample_intent(
        intent_id="intent_ops_script_approved_ready_001",
        agent="hermes",
        surface="ops-script-signing-test",
        action_class="external_send",
        channel="telegram",
        payload={"message": "signing payload must not leak", "target": "dry-run"},
        idempotency_key=key,
    )
    gateway_contract.ingest_intents([intent], gateway_dir)
    approved = gateway_contract.approve_dry_run(
        gateway_dir=gateway_dir,
        idempotency_key=key,
        approval_id="approval-signing-test-001",
        allowed_executor="fcar-gateway.mock-executor",
        readback_probe={"type": "mock", "idempotency_key": key},
    )
    assert approved["decision"] == "approval_simulated"
    return key


def test_one_shot_signer_signs_approved_ready_intent(tmp_path):
    from fcar_action_gateway_executor_signing import sign_executor_token, verify_executor_token

    key = _seed_approved_ready_gateway(tmp_path)
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret="unit-test-secret",
        ttl_seconds=300,
    )

    assert token["status"] == "SIGNED"
    assert token["external_side_effect"] is False
    assert token["claims"]["idempotency_key"] == key
    assert token["claims"]["allowed_executor"] == "fcar-gateway.mock-executor"
    assert "signature" in token

    verified = verify_executor_token(token, secret="unit-test-secret")
    assert verified["status"] == "VERIFIED"
    assert verified["external_side_effect"] is False


def test_one_shot_signer_refuses_unapproved_intent(tmp_path):
    from fcar_action_gateway_executor_signing import sign_executor_token

    key = _seed_gateway(tmp_path)
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret="unit-test-secret",
        ttl_seconds=300,
    )

    assert token["status"] == "SIGN_BLOCKED"
    assert "ready_to_execute" in token["reason"]
    assert token["external_side_effect"] is False


def test_one_shot_verifier_rejects_tampered_executor(tmp_path):
    from fcar_action_gateway_executor_signing import sign_executor_token, verify_executor_token

    key = _seed_approved_ready_gateway(tmp_path)
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret="unit-test-secret",
        ttl_seconds=300,
    )
    token["claims"]["allowed_executor"] = "attacker.executor"

    verified = verify_executor_token(token, secret="unit-test-secret")
    assert verified["status"] == "VERIFY_FAILED"
    assert verified["external_side_effect"] is False


def test_one_shot_verifier_rejects_expired_token(tmp_path):
    from fcar_action_gateway_executor_signing import sign_executor_token, verify_executor_token

    key = _seed_approved_ready_gateway(tmp_path)
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret="unit-test-secret",
        ttl_seconds=-1,
    )

    verified = verify_executor_token(token, secret="unit-test-secret")
    assert verified["status"] == "TOKEN_EXPIRED"
    assert verified["external_side_effect"] is False


def test_one_shot_consume_token_appends_consumed_ledger_entry(tmp_path):
    from fcar_action_gateway_executor_signing import (
        consume_executor_token,
        sign_executor_token,
        token_id_from_claims,
    )

    key = _seed_approved_ready_gateway(tmp_path)
    secret = "unit-test-secret"
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret=secret,
        ttl_seconds=300,
    )

    result = consume_executor_token(
        gateway_dir=tmp_path,
        token=token,
        secret=secret,
        expected_executor="fcar-gateway.mock-executor",
    )

    assert result["status"] == "TOKEN_CONSUMED"
    assert result["external_side_effect"] is False
    assert result["dry_run_only"] is True
    assert result["token_id"] == token_id_from_claims(token["claims"])

    entries = gateway_contract.read_jsonl(gateway_contract.ledger_path(tmp_path))
    consumed = [entry for entry in entries if entry.get("entry_type") == "executor_token_consumed"]
    assert len(consumed) == 1
    assert consumed[0]["token_id"] == result["token_id"]
    assert consumed[0]["record"]["idempotency_key"] == key
    assert gateway_contract.audit_ledger(tmp_path)["status"] == "AUDIT_PASS"


def test_one_shot_consume_token_rejects_replay(tmp_path):
    from fcar_action_gateway_executor_signing import consume_executor_token, sign_executor_token

    key = _seed_approved_ready_gateway(tmp_path)
    secret = "unit-test-secret"
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret=secret,
        ttl_seconds=300,
    )

    first = consume_executor_token(
        gateway_dir=tmp_path,
        token=token,
        secret=secret,
        expected_executor="fcar-gateway.mock-executor",
    )
    replay = consume_executor_token(
        gateway_dir=tmp_path,
        token=token,
        secret=secret,
        expected_executor="fcar-gateway.mock-executor",
    )

    assert first["status"] == "TOKEN_CONSUMED"
    assert replay["status"] == "TOKEN_ALREADY_CONSUMED"
    assert replay["external_side_effect"] is False
    entries = gateway_contract.read_jsonl(gateway_contract.ledger_path(tmp_path))
    assert sum(1 for entry in entries if entry.get("entry_type") == "executor_token_consumed") == 1


def test_one_shot_consume_token_rejects_tampered_token(tmp_path):
    from fcar_action_gateway_executor_signing import consume_executor_token, sign_executor_token

    key = _seed_approved_ready_gateway(tmp_path)
    secret = "unit-test-secret"
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret=secret,
        ttl_seconds=300,
    )
    token["claims"]["allowed_executor"] = "attacker.executor"

    result = consume_executor_token(
        gateway_dir=tmp_path,
        token=token,
        secret=secret,
        expected_executor="fcar-gateway.mock-executor",
    )

    assert result["status"] == "VERIFY_FAILED"
    assert result["external_side_effect"] is False
    entries = gateway_contract.read_jsonl(gateway_contract.ledger_path(tmp_path))
    assert not any(entry.get("entry_type") == "executor_token_consumed" for entry in entries)


def test_one_shot_consume_token_rejects_expired_token(tmp_path):
    from fcar_action_gateway_executor_signing import consume_executor_token, sign_executor_token

    key = _seed_approved_ready_gateway(tmp_path)
    secret = "unit-test-secret"
    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret=secret,
        ttl_seconds=-1,
    )

    result = consume_executor_token(
        gateway_dir=tmp_path,
        token=token,
        secret=secret,
        expected_executor="fcar-gateway.mock-executor",
    )

    assert result["status"] == "TOKEN_EXPIRED"
    assert result["external_side_effect"] is False
    entries = gateway_contract.read_jsonl(gateway_contract.ledger_path(tmp_path))
    assert not any(entry.get("entry_type") == "executor_token_consumed" for entry in entries)


def test_disabled_executor_requires_consumed_token(tmp_path):
    from fcar_action_gateway_executor_adapters import execute_internal_telegram_alert_disabled
    from fcar_action_gateway_executor_signing import sign_executor_token

    key = _seed_approved_ready_gateway(tmp_path)
    secret = "unit-test-secret"

    missing = execute_internal_telegram_alert_disabled(
        gateway_dir=tmp_path,
        idempotency_key=key,
        token=None,
        token_secret=None,
        chat_id="123",
        expected_executor="fcar-gateway.mock-executor",
    )
    assert missing["status"] == "TOKEN_REQUIRED"
    assert missing["external_side_effect"] is False

    token = sign_executor_token(
        gateway_dir=tmp_path,
        idempotency_key=key,
        allowed_executor="fcar-gateway.mock-executor",
        secret=secret,
        ttl_seconds=300,
    )
    disabled = execute_internal_telegram_alert_disabled(
        gateway_dir=tmp_path,
        idempotency_key=key,
        token=token,
        token_secret=secret,
        chat_id="123",
        expected_executor="fcar-gateway.mock-executor",
    )
    replay = execute_internal_telegram_alert_disabled(
        gateway_dir=tmp_path,
        idempotency_key=key,
        token=token,
        token_secret=secret,
        chat_id="123",
        expected_executor="fcar-gateway.mock-executor",
    )

    assert disabled["status"] == "EXECUTOR_DISABLED"
    assert disabled["token_status"] == "TOKEN_CONSUMED"
    assert disabled["external_side_effect"] is False
    assert replay["status"] == "TOKEN_ALREADY_CONSUMED"
    assert replay["external_side_effect"] is False


def test_raw_live_executor_manifest_without_guardrails_fails_audit(tmp_path):
    gateway_contract.write_manifest(tmp_path)
    manifest_path = gateway_contract.manifest_path(tmp_path)
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    manifest["live_executors_enabled"] = True
    manifest_path.write_text(__import__("json").dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    audit = gateway_contract.audit_ledger(tmp_path)

    assert audit["status"] == "AUDIT_FAIL"
    assert "production_guardrails_missing" in audit["errors"]


def test_guarded_production_enable_passes_audit_without_external_side_effect(tmp_path):
    gateway_contract.write_manifest(tmp_path)

    result = gateway_contract.set_live_executors_enabled(
        gateway_dir=tmp_path,
        enabled=True,
        operator="test-operator",
        reason="unit test guarded enable",
        burn_in={"tests_passed": 50, "emulations_passed": 50},
        evidence_artifact="unit-test-artifact",
    )
    audit = gateway_contract.audit_ledger(tmp_path)

    assert result["status"] == "PRODUCTION_ENABLED"
    assert result["external_side_effect"] is False
    assert audit["status"] == "AUDIT_PASS"
    manifest = __import__("json").loads(gateway_contract.manifest_path(tmp_path).read_text(encoding="utf-8"))
    assert manifest["live_executors_enabled"] is True
    assert manifest["production_guardrails"]["one_shot_executor_tokens_required"] is True
    assert manifest["production_guardrails"]["burn_in"]["tests_passed"] == 50
    assert manifest["production_guardrails"]["burn_in"]["emulations_passed"] == 50


def test_production_burnin_runs_emulations_without_external_side_effect(tmp_path):
    from fcar_action_gateway_production_burnin import run_emulation_burnin

    result = run_emulation_burnin(gateway_dir=tmp_path, count=3, enable_production=True)

    assert result["status"] == "PASS"
    assert result["requested_count"] == 3
    assert result["passed_count"] == 3
    assert result["external_side_effect"] is False
    assert result["audit"]["status"] == "AUDIT_PASS"
