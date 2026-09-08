"""Portable boundaries for host-owned FCAR and Instar integrations."""

import builtins
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock

import pytest


def test_fcar_command_parser_loads_without_host_gateway_modules(monkeypatch, tmp_path):
    from gateway import fcar_action_gateway_commands as commands

    monkeypatch.setattr(commands, "SCRIPTS_DIR", tmp_path / "missing")
    monkeypatch.setitem(sys.modules, "fcar_action_gateway_contract", None)
    monkeypatch.setitem(sys.modules, "fcar_action_gateway_review", None)

    assert commands.parse_fcar_gateway_command("/approve") is None
    assert commands.parse_fcar_gateway_command("/fcar_gateway_list") is not None
    result = commands.handle_fcar_gateway_command("/fcar_gateway_list", gateway_dir=tmp_path)
    assert result.consumed is True
    assert "failed closed: ModuleNotFoundError" in result.text


@pytest.mark.asyncio
async def test_missing_instar_bridge_does_not_consume_unrelated_text(monkeypatch, tmp_path):
    from plugins.platforms.telegram import adapter as telegram_module

    adapter = object.__new__(telegram_module.TelegramAdapter)
    event = SimpleNamespace(
        text="ordinary message",
        message_id="1",
        source=SimpleNamespace(chat_id="123", user_id="456"),
    )
    real_path = Path
    monkeypatch.setattr(
        telegram_module,
        "_Path",
        lambda value: tmp_path / "missing" if value == "/home/info/scripts" else real_path(value),
    )
    monkeypatch.setitem(sys.modules, "hermes_instar_decision_bridge", None)

    assert await adapter._maybe_handle_instar_loop_decision(event) is False


@pytest.mark.asyncio
async def test_missing_instar_bridge_does_not_consume_unrelated_callback(monkeypatch, tmp_path):
    from plugins.platforms.telegram import adapter as telegram_module

    adapter = object.__new__(telegram_module.TelegramAdapter)
    query = SimpleNamespace(answer=AsyncMock(), message=None, from_user=None)
    real_path = Path
    monkeypatch.setattr(
        telegram_module,
        "_Path",
        lambda value: tmp_path / "missing" if value == "/home/info/scripts" else real_path(value),
    )
    monkeypatch.setitem(sys.modules, "hermes_instar_decision_bridge", None)

    consumed = await adapter._maybe_handle_instar_loop_callback(
        query,
        "unrelated:callback",
        query_chat_id="123",
        query_thread_id=None,
    )

    assert consumed is False
    query.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_nested_instar_module_fails_closed(monkeypatch, tmp_path):
    from plugins.platforms.telegram import adapter as telegram_module

    adapter = object.__new__(telegram_module.TelegramAdapter)
    event = SimpleNamespace(
        text="ordinary message",
        message_id="1",
        source=SimpleNamespace(chat_id="123", user_id="456"),
    )
    real_import = builtins.__import__

    def import_with_missing_nested_module(name, *args, **kwargs):
        if name == "hermes_instar_decision_bridge":
            raise ModuleNotFoundError(name="instar_loop_decision")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(telegram_module, "_Path", lambda _value: tmp_path / "missing")
    monkeypatch.setattr(builtins, "__import__", import_with_missing_nested_module)

    assert await adapter._maybe_handle_instar_loop_decision(event) is True


@pytest.mark.asyncio
async def test_instar_handler_module_error_fails_closed(monkeypatch, tmp_path):
    from plugins.platforms.telegram import adapter as telegram_module

    def raise_handler_module_error(*_args, **_kwargs):
        raise ModuleNotFoundError(name="hermes_instar_decision_bridge")

    adapter = object.__new__(telegram_module.TelegramAdapter)
    query = SimpleNamespace(answer=AsyncMock(), message=None, from_user=None)
    bridge = SimpleNamespace(handle_callback=raise_handler_module_error)
    monkeypatch.setattr(telegram_module, "_Path", lambda _value: tmp_path / "missing")
    monkeypatch.setitem(sys.modules, "hermes_instar_decision_bridge", bridge)

    consumed = await adapter._maybe_handle_instar_loop_callback(
        query,
        "unrelated:callback",
        query_chat_id="123",
        query_thread_id=None,
    )

    assert consumed is True
    query.answer.assert_awaited_once_with(text="Instar decision failed closed.")
