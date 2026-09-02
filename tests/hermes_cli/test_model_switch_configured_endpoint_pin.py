"""Regression tests: a Claude-family `/model` name must never leave the
configured proxy lane (incident 2026-09-02, Gold Finger RC run).

Repro: a Telegram session had failed over to the codex fallback
(``openai-codex``).  Typing ``/model claude-opus-5`` let ``switch_model`` step e
(``detect_provider_for_model``) detect ``anthropic`` from the static catalog and
pin the session to ``provider=anthropic base_url=https://api.anthropic.com`` —
bypassing the operator's configured proxy
(``model.provider=custom``, ``base_url=http://127.0.0.1:3456/passthrough``,
``api_mode=anthropic_messages``).  Max-subscription traffic then billed as API,
and the proxy-only alias ``fable`` 404'd on every turn from that session.

The fix adds step d.7 ("configured-endpoint pin") between d.5 and e: when
nothing earlier resolved the name and the typed name is Claude-family, route it
to the configured Anthropic-messages proxy lane and skip step e.

Hermetic: the whole resolution chain is mocked (no network), mirroring
``tests/hermes_cli/test_model_switch_configured_provider_routing.py``.
"""

from unittest.mock import patch

import pytest

from hermes_cli.model_switch import (
    ConfigLaneUnavailable,
    ProxyLane,
    configured_anthropic_proxy_lane,
    is_claude_family_model,
    strip_anthropic_prefix,
    switch_model,
)

PROXY_BASE_URL = "http://127.0.0.1:3456/passthrough"

_ACCEPTED = {"accepted": True, "persist": True, "recognized": True, "message": None}

# The operator's real config.yaml `model:` block (the configured proxy lane).
_PROXY_LANE_CONFIG = {
    "model": {
        "provider": "custom",
        "base_url": PROXY_BASE_URL,
        "default": "claude-fable-5-1",
        "api_mode": "anthropic_messages",
        "context_length": 1000000,
    }
}

# A config whose default lane is vendor-direct — the pin must be a no-op.
_ANTHROPIC_LANE_CONFIG = {
    "model": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "default": "claude-opus-5",
        "api_mode": "anthropic_messages",
    }
}

# Distinct from PROXY_BASE_URL so an assertion can tell "pinned" from
# "resolved the ordinary way".
_UNPINNED_BASE_URL = "http://resolved/v1"


def _runtime(**kwargs):
    """Stand-in for resolve_runtime_provider that echoes an explicit base_url."""
    return {
        "api_key": "sk-from-keychain",
        "base_url": kwargs.get("explicit_base_url") or _UNPINNED_BASE_URL,
        "api_mode": "",
    }


def _run_switch(
    *,
    raw_input,
    current_provider,
    current_model="old-model",
    current_base_url="",
    config=_PROXY_LANE_CONFIG,
    detected=None,
    alias_result=None,
    lane_side_effect=None,
):
    """Drive ``switch_model`` with every external lookup patched out.

    ``detect_provider_for_model`` returns *detected* so a test can prove step e
    is skipped (the incident's exact mechanism) rather than merely inert.
    ``alias_result`` is what ``resolve_alias`` returns — the (provider, model,
    alias) triple that lets step a land a name on a provider before the pin.
    ``lane_side_effect`` replaces the lane lookup (used to inject a config read
    failure at the seam; patching ``load_config`` itself is swallowed by the
    broad ``except Exception`` around PATH B's moa lookup).
    """
    lane_patch = (
        patch("hermes_cli.model_switch.configured_anthropic_proxy_lane",
              side_effect=lane_side_effect)
        if lane_side_effect is not None
        else patch("hermes_cli.config.load_config", return_value=config)
    )
    with patch("hermes_cli.model_switch.resolve_alias", return_value=alias_result), \
         patch("hermes_cli.model_switch.list_provider_models", return_value=[]), \
         patch(
             "hermes_cli.model_switch.normalize_model_for_provider",
             side_effect=lambda model, provider: model,
         ), \
         patch("hermes_cli.models.validate_requested_model", return_value=_ACCEPTED), \
         patch("hermes_cli.models.detect_provider_for_model", return_value=detected), \
         patch("hermes_cli.model_switch.get_model_info", return_value=None), \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None), \
         lane_patch, \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             side_effect=lambda **kw: _runtime(**kw),
         ):
        return switch_model(
            raw_input=raw_input,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key="sk-current-session",
            user_providers={},
            custom_providers=[],
        )


# ── The helper the pin is built on ─────────────────────────────────────

def test_is_claude_family_model_matches_family_and_aliases():
    for name in (
        "claude-opus-5", "claude-fable-5-1", "fable", "opus", "sonnet",
        "haiku", "Claude-Sonnet-4-5", "opus:latest", "fable_5",
    ):
        assert is_claude_family_model(name) is True, name


def test_is_claude_family_model_rejects_other_vendors():
    for name in (
        "gpt-5.6-sol", "gpt-5.6-sol-900k", "claudia-7b", "fabletown-1",
        "opusnet", "deepseek-v4-flash", "", None,
    ):
        assert is_claude_family_model(name) is False, name


def test_configured_lane_requires_custom_anthropic_messages_endpoint():
    with patch("hermes_cli.config.load_config", return_value=_PROXY_LANE_CONFIG):
        assert configured_anthropic_proxy_lane() == ProxyLane(
            provider="custom",
            base_url=PROXY_BASE_URL,
            api_mode="anthropic_messages",
            api_key="",
        )
    with patch("hermes_cli.config.load_config", return_value=_ANTHROPIC_LANE_CONFIG):
        assert configured_anthropic_proxy_lane() is None


def test_unreadable_config_is_not_no_lane():
    """A config that cannot be READ must be distinguishable from a config that
    declares no proxy — collapsing them fails OPEN on the bypass."""
    with patch("hermes_cli.config.load_config", side_effect=OSError("boom")):
        with pytest.raises(ConfigLaneUnavailable):
            configured_anthropic_proxy_lane()


@pytest.mark.parametrize(
    "spelling", ["anthropic_messages", "anthropic-messages", "anthropic", "messages",
                 "Anthropic_Messages", "  anthropic-messages  "],
)
def test_api_mode_spellings_all_mean_the_same_wire_protocol(spelling):
    cfg = {"model": {"provider": "custom", "base_url": PROXY_BASE_URL, "api_mode": spelling}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        lane = configured_anthropic_proxy_lane()
    assert lane is not None, spelling
    # Canonicalised on the way out, whatever the operator typed.
    assert lane.api_mode == "anthropic_messages"


def test_chat_completions_lane_is_not_an_anthropic_proxy():
    cfg = {"model": {"provider": "custom", "base_url": PROXY_BASE_URL,
                     "api_mode": "chat_completions"}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        assert configured_anthropic_proxy_lane() is None


def test_strip_anthropic_prefix():
    assert strip_anthropic_prefix("anthropic/claude-opus-5") == "claude-opus-5"
    assert strip_anthropic_prefix("Anthropic/claude-opus-5") == "claude-opus-5"
    assert strip_anthropic_prefix("fable") == "fable"
    assert strip_anthropic_prefix("openrouter/anthropic/claude") == "openrouter/anthropic/claude"


# ── (a) the incident: codex fallback + /model claude-opus-5 ────────────

def test_claude_name_from_codex_fallback_pins_to_configured_proxy():
    """The 2026-08-31 11:35 hop ("switched from gpt-5.6-sol-900k to
    claude-opus-5 via Anthropic") must land on the configured proxy instead."""
    result = _run_switch(
        raw_input="claude-opus-5",
        current_provider="openai-codex",
        current_model="gpt-5.6-sol-900k",
        current_base_url="https://chatgpt.com/backend-api/codex",
        detected=("anthropic", "claude-opus-5"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL
    assert result.new_model == "claude-opus-5"
    assert result.api_mode == "anthropic_messages"


# ── (b) the 404 loop: anthropic-direct session + /model fable ──────────

def test_proxy_only_alias_from_anthropic_direct_session_is_pinned_back():
    """``fable`` exists in no catalog and 404s on api.anthropic.com; the pin
    routes it back to the proxy that actually serves it."""
    result = _run_switch(
        raw_input="fable",
        current_provider="anthropic",
        current_model="claude-opus-5",
        current_base_url="https://api.anthropic.com",
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL
    assert result.new_model == "fable"


# ── (c) non-Claude names are untouched ─────────────────────────────────

def test_non_claude_name_is_not_pinned():
    result = _run_switch(
        raw_input="gpt-5.6-sol",
        current_provider="custom",
        current_model="claude-fable-5-1",
        current_base_url=PROXY_BASE_URL,
    )
    assert result.success is True, result.error_message
    assert result.new_model == "gpt-5.6-sol"
    # Resolved the ordinary way (mocked resolver), not forced onto the lane.
    assert result.base_url == _UNPINNED_BASE_URL


# ── (d) no configured proxy lane -> step is a no-op ────────────────────

def test_no_op_when_configured_lane_is_not_a_custom_proxy():
    result = _run_switch(
        raw_input="claude-opus-5",
        current_provider="openai-codex",
        current_model="gpt-5.6-sol-900k",
        config=_ANTHROPIC_LANE_CONFIG,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "openai-codex"
    assert result.base_url != PROXY_BASE_URL


# ── Round 2: the invariant, for names an EARLIER step already resolved ──
#
# Step d.7 alone is bypassable — it only fires when nothing resolved the name.
# These pin the post-resolution invariant at the end of PATH B.

def test_bare_claude_alias_from_anthropic_session_is_pinned_back():
    """MODEL_ALIASES maps bare `claude` to vendor `anthropic`, so step a
    resolves it and step d.7 never runs. The invariant must still catch it."""
    result = _run_switch(
        raw_input="claude",
        current_provider="anthropic",
        current_model="claude-opus-5",
        current_base_url="https://api.anthropic.com",
        alias_result=("anthropic", "claude-opus-4-5", "claude"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL
    assert result.api_mode == "anthropic_messages"


def test_openrouter_style_vendor_slug_is_pinned_and_prefix_stripped():
    """`anthropic/claude-opus-5` does not read as Claude-family without the
    vendor-prefix allowance; the proxy also needs the bare id."""
    result = _run_switch(
        raw_input="anthropic/claude-opus-5",
        current_provider="openai-codex",
        current_model="gpt-5.6-sol-900k",
        detected=("anthropic", "anthropic/claude-opus-5"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL
    assert result.new_model == "claude-opus-5"


def test_claude_resolved_onto_openrouter_is_pinned_back():
    """OpenRouter serves Claude on a metered vendor bill — same bypass class."""
    result = _run_switch(
        raw_input="claude-opus-5",
        current_provider="openrouter",
        current_model="anthropic/claude-sonnet-4",
        alias_result=("openrouter", "anthropic/claude-opus-5", "opus"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL
    assert result.new_model == "claude-opus-5"


def test_non_claude_name_on_anthropic_session_is_left_alone():
    """The invariant keys on the MODEL, not on the session's provider."""
    result = _run_switch(
        raw_input="gpt-5.6-sol",
        current_provider="anthropic",
        current_model="claude-opus-5",
        current_base_url="https://api.anthropic.com",
        detected=None,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "anthropic"
    assert result.base_url != PROXY_BASE_URL


def test_unreadable_config_refuses_to_route_a_claude_name():
    """Fail CLOSED: we cannot prove where the configured lane points."""
    result = _run_switch(
        raw_input="claude-opus-5",
        current_provider="openai-codex",
        current_model="gpt-5.6-sol-900k",
        lane_side_effect=ConfigLaneUnavailable("permission denied"),
    )
    assert result.success is False
    assert "config.yaml unreadable" in result.error_message
    assert "refusing to route a Claude model off the configured lane" in result.error_message


def test_unreadable_config_does_not_block_a_non_claude_name():
    """The lane is only consulted for Claude-family names, so a gpt switch on
    an unreadable config behaves exactly as before."""
    result = _run_switch(
        raw_input="gpt-5.6-sol",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        lane_side_effect=ConfigLaneUnavailable("permission denied"),
    )
    assert result.success is True, result.error_message
    assert result.new_model == "gpt-5.6-sol"


def test_configured_lane_api_key_is_used():
    cfg = {
        "model": {
            "provider": "custom",
            "base_url": PROXY_BASE_URL,
            "api_mode": "anthropic_messages",
            "api_key": "sk-proxy-lane-key",
        }
    }
    result = _run_switch(
        raw_input="fable",
        current_provider="anthropic",
        current_model="claude-opus-5",
        current_base_url="https://api.anthropic.com",
        config=cfg,
    )
    assert result.success is True, result.error_message
    assert result.api_key == "sk-proxy-lane-key"
    assert result.base_url == PROXY_BASE_URL


def test_env_reference_in_configured_api_key_is_expanded(monkeypatch):
    monkeypatch.setenv("PROXY_LANE_KEY", "sk-from-env")
    cfg = {
        "model": {
            "provider": "custom",
            "base_url": PROXY_BASE_URL,
            "api_mode": "anthropic_messages",
            "api_key": "${PROXY_LANE_KEY}",
        }
    }
    result = _run_switch(
        raw_input="fable",
        current_provider="anthropic",
        current_model="claude-opus-5",
        config=cfg,
    )
    assert result.success is True, result.error_message
    assert result.api_key == "sk-from-env"
