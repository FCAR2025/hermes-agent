"""Telegram integration for FCAR Action Gateway command lane.

The FCAR lane must not intercept Hermes native /approve or /deny.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_SCRIPTS = "/home/info/scripts"
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import fcar_action_gateway_contract as gateway_contract  # noqa: E402
from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platforms.base import MessageEvent, MessageType  # noqa: E402
from gateway.session import SessionSource  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _seed_gateway(gateway_dir: Path) -> str:
    key = "telegram-integration:fcar-gateway:pending:001"
    gateway_contract.write_manifest(gateway_dir)
    intent = gateway_contract.sample_intent(
        intent_id="intent_telegram_integration_001",
        agent="hermes",
        surface="telegram-integration-test",
        action_class="external_send",
        channel="telegram",
        payload={"message": "payload must not leak"},
        idempotency_key=key,
    )
    gateway_contract.ingest_intents([intent], gateway_dir)
    return key


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="777",
            user_name="Carl",
            message_id="99",
        ),
        message_id="99",
    )


@pytest.mark.asyncio
async def test_maybe_handle_fcar_gateway_command_sends_review_response(tmp_path):
    key = _seed_gateway(tmp_path)
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(enabled=True, token="test", extra={"fcar_action_gateway_dir": str(tmp_path)})
    adapter._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=42))

    consumed = await adapter._maybe_handle_fcar_gateway_command(_event("/fcar_gateway_list"))

    assert consumed is True
    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert key in kwargs["text"]
    assert "payload must not leak" not in kwargs["text"]


@pytest.mark.asyncio
async def test_maybe_handle_fcar_gateway_command_ignores_hermes_native_approve(tmp_path):
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(enabled=True, token="test", extra={"fcar_action_gateway_dir": str(tmp_path)})
    adapter._send_message_with_thread_fallback = AsyncMock()

    consumed = await adapter._maybe_handle_fcar_gateway_command(_event("/approve"))

    assert consumed is False
    adapter._send_message_with_thread_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_fcar_import_failure_does_not_consume_native_approve(tmp_path, monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(enabled=True, token="test", extra={"fcar_action_gateway_dir": str(tmp_path)})
    adapter._send_message_with_thread_fallback = AsyncMock()
    monkeypatch.setitem(sys.modules, "gateway.fcar_action_gateway_commands", None)

    consumed = await adapter._maybe_handle_fcar_gateway_command(_event("/approve"))

    assert consumed is False
    adapter._send_message_with_thread_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_command_consumes_fcar_gateway_before_llm_dispatch():
    adapter = object.__new__(TelegramAdapter)
    msg = SimpleNamespace(text="/fcar_gateway_status")
    update = SimpleNamespace(update_id=123, effective_message=msg, message=msg)
    adapter._effective_update_message = lambda _update: msg
    adapter._should_process_message = lambda _msg, is_command=False: True
    adapter._ensure_forum_commands = AsyncMock()
    adapter._build_message_event = MagicMock(return_value=_event("/fcar_gateway_status"))
    adapter._clean_bot_trigger_text = lambda text: text
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter._maybe_handle_fcar_gateway_command = AsyncMock(return_value=True)
    adapter.handle_message = AsyncMock()

    await adapter._handle_command(update, MagicMock())

    adapter._maybe_handle_fcar_gateway_command.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_handle_fcar_gateway_command_uses_fcar_operator_allowlist(tmp_path):
    key = _seed_gateway(tmp_path)
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "fcar_action_gateway_dir": str(tmp_path),
            "fcar_action_gateway_operators": ["telegram:777"],
        },
    )
    adapter._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=42))

    consumed = await adapter._maybe_handle_fcar_gateway_command(_event(f"/fcar_gateway_approve {key}"))

    assert consumed is True
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Dry-run approved" in kwargs["text"]
    audit = gateway_contract.audit_ledger(tmp_path)
    assert audit["status"] == "AUDIT_PASS"
