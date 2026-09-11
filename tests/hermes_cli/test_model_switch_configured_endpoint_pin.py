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

import contextlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

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


@contextlib.contextmanager
def _config_file(config):
    """Point ``get_config_path()`` at a real config.yaml holding *config*.

    The lane does a STRICT read of the raw document (never ``load_config()``,
    which falls back to defaults and would make an unreadable config look like
    "no proxy lane"), so tests must supply a real file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        with patch("hermes_cli.config.get_config_path", return_value=path):
            yield path


@contextlib.contextmanager
def _missing_config_file():
    """Point ``get_config_path()`` at a path that does not exist.

    This is NOT the fail-closed case: a config that was never written declares
    no lane, and refusing here would break every Claude switch on a fresh
    install (and on the test suite's sandboxed HERMES_HOME).
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "does-not-exist" / "config.yaml"
        with patch("hermes_cli.config.get_config_path", return_value=path):
            yield path


@contextlib.contextmanager
def _unreadable_config_file():
    """Point ``get_config_path()`` at something that EXISTS but cannot be read.

    A directory at the config path raises IsADirectoryError deterministically,
    regardless of the running user's privileges (a chmod-000 file would not:
    root ignores it).
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.mkdir()
        with patch("hermes_cli.config.get_config_path", return_value=path):
            yield path


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
    ``config`` is written to a real temp config.yaml that ``get_config_path()``
    is pointed at, so the lane's strict read is genuinely exercised.
    ``lane_side_effect`` replaces the lane lookup outright, for tests that need
    a specific exception rather than a specific file.
    """
    lane_patch = (
        patch("hermes_cli.model_switch.configured_anthropic_proxy_lane",
              side_effect=lane_side_effect)
        if lane_side_effect is not None
        else _config_file(config)
    )
    with patch("hermes_cli.model_switch.resolve_alias", return_value=alias_result), \
         patch("hermes_cli.model_switch.list_provider_models", return_value=[]), \
         patch(
             "hermes_cli.model_switch.normalize_model_for_provider",
             side_effect=lambda model, provider: model,
         ), \
         patch("hermes_cli.models_validate.validate_requested_model", return_value=_ACCEPTED), \
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
    with _config_file(_PROXY_LANE_CONFIG):
        assert configured_anthropic_proxy_lane() == ProxyLane(
            provider="custom",
            base_url=PROXY_BASE_URL,
            api_mode="anthropic_messages",
            api_key="",
        )
    with _config_file(_ANTHROPIC_LANE_CONFIG):
        assert configured_anthropic_proxy_lane() is None


def test_unreadable_config_is_not_no_lane():
    """A config that EXISTS but cannot be READ must be distinguishable from one
    that declares no proxy — collapsing them fails OPEN on the bypass.

    ``load_config()`` cannot express this: it falls back to DEFAULT_CONFIG on a
    corrupt or unreadable file, so the lane does its own strict read.
    """
    with _unreadable_config_file():
        with pytest.raises(ConfigLaneUnavailable):
            configured_anthropic_proxy_lane()


def test_missing_config_file_is_no_lane_not_a_refusal():
    """A config that was never written declares no lane. Refusing here would
    break every Claude switch on a fresh install — and the test suite's own
    sandboxed HERMES_HOME, which holds no config.yaml."""
    with _missing_config_file():
        assert configured_anthropic_proxy_lane() is None


def test_load_config_fallback_does_not_mask_an_unreadable_file():
    """Pins WHY the strict read exists: the tolerant loader hands back a usable
    dict for a config it could not read, which reads as "no proxy lane"."""
    from hermes_cli.config import load_config

    with _unreadable_config_file():
        assert isinstance(load_config(), dict)   # tolerant: no raise
        with pytest.raises(ConfigLaneUnavailable):
            configured_anthropic_proxy_lane()    # strict: refuses


def test_corrupt_yaml_is_unreadable_not_no_lane():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text("model: {unclosed: [1, 2\n  bad: : :\n", encoding="utf-8")
        with patch("hermes_cli.config.get_config_path", return_value=path):
            with pytest.raises(ConfigLaneUnavailable):
                configured_anthropic_proxy_lane()


def test_non_mapping_document_is_unreadable():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with patch("hermes_cli.config.get_config_path", return_value=path):
            with pytest.raises(ConfigLaneUnavailable):
                configured_anthropic_proxy_lane()


@pytest.mark.parametrize(
    "spelling", ["anthropic_messages", "anthropic-messages", "anthropic", "messages",
                 "Anthropic_Messages", "  anthropic-messages  "],
)
def test_api_mode_spellings_all_mean_the_same_wire_protocol(spelling):
    cfg = {"model": {"provider": "custom", "base_url": PROXY_BASE_URL, "api_mode": spelling}}
    with _config_file(cfg):
        lane = configured_anthropic_proxy_lane()
    assert lane is not None, spelling
    # Canonicalised on the way out, whatever the operator typed.
    assert lane.api_mode == "anthropic_messages"


def test_chat_completions_lane_is_not_an_anthropic_proxy():
    cfg = {"model": {"provider": "custom", "base_url": PROXY_BASE_URL,
                     "api_mode": "chat_completions"}}
    with _config_file(cfg):
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


# ── Round 3: any provider that is not the lane, judged on the RESOLVED name ──

def test_claude_resolved_onto_nous_is_pinned_back():
    """Static catalogs for nous / opencode-* / openai-codex list `claude-*` ids
    too. An {anthropic, openrouter} allowlist left each of them an open bypass;
    the test is "not the configured lane", not a list of known vendors."""
    result = _run_switch(
        raw_input="opus",
        current_provider="openai-codex",
        current_model="gpt-5.6-sol-900k",
        alias_result=("nous", "claude-opus-5", "opus"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL
    assert result.new_model == "claude-opus-5"


def test_claude_resolved_onto_another_custom_slug_is_pinned_back():
    """`custom` and `custom:<slug>` are different endpoints — being "a custom
    provider" is not the same as being THE configured lane."""
    result = _run_switch(
        raw_input="claude-opus-5",
        current_provider="opencode-zen",
        current_model="deepseek-v4-flash",
        alias_result=("custom:some-other-proxy", "claude-opus-5", "opus"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL


def test_switch_already_on_the_lane_is_not_re_pinned():
    result = _run_switch(
        raw_input="claude-fable-5-1",
        current_provider="custom",
        current_model="fable",
        current_base_url=PROXY_BASE_URL,
        alias_result=("custom", "claude-fable-5-1", "fable"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"


def test_alias_resolving_to_a_non_claude_model_is_not_re_routed():
    """Judged on the RESOLVED model, not on what the operator typed: a
    `model_aliases` entry mapping `opus` to a non-Claude id on another provider
    is that provider's traffic, not Claude traffic."""
    result = _run_switch(
        raw_input="opus",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        alias_result=("opencode-zen", "deepseek-v4-flash", "opus"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "opencode-zen"
    assert result.new_model == "deepseek-v4-flash"
    assert result.base_url != PROXY_BASE_URL


def test_bare_provider_name_resolving_to_a_claude_default_is_pinned():
    """`/model anthropic` is not Claude-family, but step e maps a bare provider
    name to that provider's DEFAULT model — which is claude-*. The lane had not
    been read for it, so without a top-up read the invariant was skipped."""
    result = _run_switch(
        raw_input="anthropic",
        current_provider="openai-codex",
        current_model="gpt-5.6-sol-900k",
        detected=("anthropic", "claude-opus-4-5"),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom"
    assert result.base_url == PROXY_BASE_URL
    assert result.new_model == "claude-opus-4-5"


def test_unreadable_config_file_refuses_to_route_a_claude_name():
    """End-to-end fail-closed through the real strict read (no lane mock)."""
    with _unreadable_config_file():
        with patch("hermes_cli.model_switch.resolve_alias", return_value=None), \
             patch("hermes_cli.model_switch.list_provider_models", return_value=[]), \
             patch("hermes_cli.models_validate.validate_requested_model", return_value=_ACCEPTED), \
             patch("hermes_cli.models.detect_provider_for_model", return_value=None):
            result = switch_model(
                raw_input="claude-opus-5",
                current_provider="openai-codex",
                current_model="gpt-5.6-sol-900k",
            )
    assert result.success is False
    assert "config.yaml unreadable" in result.error_message
