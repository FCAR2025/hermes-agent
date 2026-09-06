"""A persisted vendor-direct `/model` override must be healed on rehydrate.

Incident 2026-09-02 (Gold Finger RC run): a session that had drifted onto the
codex fallback typed ``/model claude-opus-5``; ``switch_model`` step e detected
``anthropic`` from the static catalog and pinned the session to
``provider=anthropic base_url=https://api.anthropic.com`` — off the operator's
configured proxy (``model.provider=custom``,
``base_url=http://127.0.0.1:3456/passthrough``, ``api_mode=anthropic_messages``).
That triple was persisted to the session store, so every gateway restart
rehydrated the bypass: Max-subscription traffic billed as API, and the
proxy-only alias ``fable`` 404'd on every turn.

``_rehydrate_session_model_override`` now rewrites such an override onto the
configured proxy lane and writes the sanitized triple back, so a restart heals
already-poisoned sessions.  Overrides for other providers are left alone.

Mirrors the harness in ``tests/gateway/test_session_model_override_persistence.py``.
"""
import contextlib
import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore

PROXY_BASE_URL = "http://127.0.0.1:3456/passthrough"

# Carl's real poisoned override (state.db gateway_routing.entry_json).
POISONED_OVERRIDE = {
    "model": "fable",
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com",
}

CODEX_OVERRIDE = {
    "model": "gpt-5.6-sol-900k",
    "provider": "openai-codex",
    "base_url": "https://chatgpt.com/backend-api/codex",
}

_PROXY_LANE_CONFIG = {
    "model": {
        "provider": "custom",
        "base_url": PROXY_BASE_URL,
        "default": "claude-fable-5-1",
        "api_mode": "anthropic_messages",
    }
}

_ANTHROPIC_LANE_CONFIG = {
    "model": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "default": "claude-opus-5",
        "api_mode": "anthropic_messages",
    }
}


@contextlib.contextmanager
def _config_file(config):
    """Point ``get_config_path()`` at a real config.yaml holding *config*.

    The lane reads the raw document strictly, so the test must supply a file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        with patch("hermes_cli.config.get_config_path", return_value=path):
            yield path


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """Build SessionStores over a shared sessions dir, without SQLite."""

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)

    def _make() -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        assert store._db is None
        return store

    return _make


def _make_runner(store):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.session_store = store
    return runner


def _rehydrate(store, session_key, *, config=_PROXY_LANE_CONFIG, lane_side_effect=None):
    """Simulated restart: fresh runner, empty in-memory overrides."""
    from hermes_cli import model_switch

    runner = _make_runner(store)
    lane_patch = (
        patch.object(model_switch, "configured_anthropic_proxy_lane",
                     side_effect=lane_side_effect)
        if lane_side_effect is not None
        else _config_file(config)
    )
    with lane_patch, \
         patch(
             "gateway.run._resolve_runtime_agent_kwargs_for_provider",
             return_value={
                 "api_key": "sk-fresh-from-keychain",
                 "api_mode": "anthropic_messages",
                 "base_url": PROXY_BASE_URL,
             },
         ):
        runner._rehydrate_session_model_override(session_key)
    return runner._session_model_overrides.get(session_key)


def _seed(store_factory, override):
    store = store_factory()
    entry = store.get_or_create_session(_make_source())
    store.set_model_override(entry.session_key, override)
    return entry.session_key


def test_anthropic_direct_override_is_sanitized_onto_configured_lane(store_factory):
    session_key = _seed(store_factory, POISONED_OVERRIDE)

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["model"] == "fable"
    assert override["provider"] == "custom"
    assert override["base_url"] == PROXY_BASE_URL
    # Credentials always come from live resolution, never from disk.
    assert override["api_key"] == "sk-fresh-from-keychain"


def test_sanitized_override_is_written_back_to_the_store(store_factory):
    session_key = _seed(store_factory, POISONED_OVERRIDE)

    store = store_factory()
    _rehydrate(store, session_key)

    # A later restart must read the healed triple, not the poisoned one.
    assert store_factory().get_model_override(session_key) == {
        "model": "fable",
        "provider": "custom",
        "base_url": PROXY_BASE_URL,
    }


def test_non_anthropic_override_is_untouched(store_factory):
    session_key = _seed(store_factory, CODEX_OVERRIDE)

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["provider"] == "openai-codex"
    assert override["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert store_factory().get_model_override(session_key) == CODEX_OVERRIDE


def test_no_op_when_configured_lane_is_not_a_custom_proxy(store_factory):
    session_key = _seed(store_factory, POISONED_OVERRIDE)

    store = store_factory()
    override = _rehydrate(store, session_key, config=_ANTHROPIC_LANE_CONFIG)

    assert override["provider"] == "anthropic"
    assert override["base_url"] == "https://api.anthropic.com"
    assert store_factory().get_model_override(session_key) == POISONED_OVERRIDE


def test_non_claude_model_on_anthropic_is_untouched(store_factory):
    session_key = _seed(
        store_factory,
        {
            "model": "gpt-5.6-sol",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
        },
    )

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["provider"] == "anthropic"
    assert override["base_url"] == "https://api.anthropic.com"


def test_unreadable_config_does_not_rehydrate_off_lane_override(store_factory, caplog):
    """Fail closed on the READ: we cannot prove where the configured lane
    points, so we neither rewrite the override nor bless the vendor-direct pin.
    The operator gets a WARNING instead of silence."""
    from hermes_cli.model_switch import ConfigLaneUnavailable

    session_key = _seed(store_factory, POISONED_OVERRIDE)

    store = store_factory()
    with caplog.at_level(logging.WARNING):
        override = _rehydrate(
            store, session_key,
            lane_side_effect=ConfigLaneUnavailable("permission denied"),
        )

    assert override is None
    assert store_factory().get_model_override(session_key) == POISONED_OVERRIDE
    assert any(
        "config.yaml unreadable" in r.getMessage() for r in caplog.records
    )


def test_same_provider_without_lane_origin_is_sanitized(store_factory):
    session_key = _seed(store_factory, {
        "model": "claude-opus-5", "provider": "custom", "base_url": "",
    })

    override = _rehydrate(store_factory(), session_key)

    assert override["provider"] == "custom"
    assert override["base_url"] == PROXY_BASE_URL


def test_vendor_slug_override_is_pinned_and_prefix_stripped(store_factory):
    """A persisted `anthropic/claude-*` slug must reach the proxy as a bare id."""
    session_key = _seed(
        store_factory,
        {
            "model": "anthropic/claude-opus-5",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
        },
    )

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["model"] == "claude-opus-5"
    assert override["provider"] == "custom"
    assert override["base_url"] == PROXY_BASE_URL


def test_api_mode_alias_in_config_still_sanitizes(store_factory):
    """The lane must not hinge on which api_mode spelling the operator used."""
    session_key = _seed(store_factory, POISONED_OVERRIDE)
    cfg = {
        "model": {
            "provider": "custom",
            "base_url": PROXY_BASE_URL,
            "api_mode": "anthropic-messages",
        }
    }

    store = store_factory()
    override = _rehydrate(store, session_key, config=cfg)

    assert override["provider"] == "custom"
    assert override["base_url"] == PROXY_BASE_URL


# ── Round 3: any provider that is not the lane, not just `anthropic` ────

NOUS_OVERRIDE = {
    "model": "claude-opus-5",
    "provider": "nous",
    "base_url": "https://inference-api.nousresearch.com/v1",
}


def test_claude_on_a_non_anthropic_provider_is_sanitized(store_factory):
    """nous/opencode-*/openai-codex catalogs list `claude-*` ids too, so a
    vendor allowlist would leave each of them an open bypass."""
    session_key = _seed(store_factory, NOUS_OVERRIDE)

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["provider"] == "custom"
    assert override["base_url"] == PROXY_BASE_URL
    assert store_factory().get_model_override(session_key) == {
        "model": "claude-opus-5",
        "provider": "custom",
        "base_url": PROXY_BASE_URL,
    }


def test_non_claude_model_on_a_non_anthropic_provider_is_untouched(store_factory):
    session_key = _seed(store_factory, CODEX_OVERRIDE)

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["provider"] == "openai-codex"
    assert store_factory().get_model_override(session_key) == CODEX_OVERRIDE


def test_override_already_on_the_lane_is_not_rewritten(store_factory):
    on_lane = {
        "model": "claude-fable-5-1",
        "provider": "custom",
        "base_url": PROXY_BASE_URL,
    }
    session_key = _seed(store_factory, on_lane)

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["provider"] == "custom"
    assert store_factory().get_model_override(session_key) == on_lane


def test_named_custom_slug_is_not_the_bare_custom_lane(store_factory):
    """`custom` and `custom:<slug>` are different endpoints; a Claude override
    parked on another custom provider still goes back to the configured one."""
    session_key = _seed(
        store_factory,
        {
            "model": "claude-opus-5",
            "provider": "custom:some-other-proxy",
            "base_url": "http://127.0.0.1:9999/v1",
        },
    )

    store = store_factory()
    override = _rehydrate(store, session_key)

    assert override["provider"] == "custom"
    assert override["base_url"] == PROXY_BASE_URL
