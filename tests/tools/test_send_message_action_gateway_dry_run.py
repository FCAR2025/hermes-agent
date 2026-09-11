import json
from pathlib import Path

from tools.send_message_tool import _handle_send


def test_send_message_action_gateway_dry_run_queues_without_platform(monkeypatch, tmp_path):
    gateway_dir = tmp_path / "gateway"
    monkeypatch.setenv("FCAR_ACTION_GATEWAY_DRY_RUN", "1")
    monkeypatch.setenv("FCAR_ACTION_GATEWAY_DIR", str(gateway_dir))
    monkeypatch.setenv("FCAR_ACTION_GATEWAY_CLI", "/home/info/scripts/fcar_action_gateway_contract.py")
    monkeypatch.setenv("FCAR_ACTION_GATEWAY_IDEMPOTENCY_KEY", "hermes:test:send-message:dry-run")

    raw = _handle_send({"target": "telegram:12345", "message": "dry run only"})
    result = json.loads(raw)

    assert result["action_gateway_dry_run"] is True
    assert result["sent"] is False
    assert result["exit_code"] == 0
    assert result["gateway_result"]["counts"] == {
        "received": 1,
        "canonical": 1,
        "duplicates": 0,
        "blocked": 0,
        "collisions": 0,
    }

    ledger = [json.loads(line) for line in (gateway_dir / "ledger.jsonl").read_text().splitlines()]
    assert len(ledger) == 1
    entry = ledger[0]
    assert entry["entry_type"] == "canonical_intent"
    assert entry["dry_run_only"] is True
    assert entry["external_side_effect"] is False
    assert entry["record"]["initiator"]["agent"] == "hermes"
    assert entry["record"]["action_class"] == "external_send"
    assert entry["record"]["channel"] == "telegram"
    assert entry["record"]["status"] == "needs_approval"
    assert entry["payload"]["source_tool"] == "hermes.send_message"
