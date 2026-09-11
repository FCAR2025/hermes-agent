"""Retapable emulation of the "Joy goes silent" stall (2026-06-07).

Reproduces the empty-response → fallback-exhaustion → degraded-model-inheritance
→ infinite-background-respin cascade that took Joy silent on 2026-06-07 (session
20260607_125850_b6470b80).  See docs/runbooks/hermes-fleet-golden-path.md and the
root-cause table in memory.

The failure chain, distilled to invariants this file LOCKS:

1. A turn that empty-exhausts the whole fallback chain leaves the agent pinned to
   the last (dead) fallback model on the dead proxy base_url.            -> FIX 2/3
2. Background autonomous jobs (bg-review / skill curation) REUSE that agent and so
   inherit the dead fallback instead of restoring the primary.           -> FIX 3
3. Nothing counts consecutive empty-exhaustions, so the autonomous loop re-fires
   into the dead transport forever (the visible "stall").                -> FIX 3
4. The fallback chain has model diversity but NOT proxy diversity: every entry is
   on the SAME proxy, so when that proxy empty-exhausts, walking the chain is
   pointless churn.                                                      -> FIX 4
5. A partial-stream timeout that recovered 0 chars must surface as a retryable
   error, NOT a content=None / finish_reason=length "stub" that the loop mistakes
   for a legitimately-empty model turn.                                  -> FIX 1

These tests are RED on the pre-fix code and GREEN once the fixes land.  They are
the deterministic, re-runnable ("retapable") emulation; the live-socket replay
emulation lives in tests/fakes/fake_stall_proxy.py + the integration test below.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


# The dead proxy from the incident (all fallbacks routed here).
DEAD_PROXY = "http://127.0.0.1:51199/v1"
PRIMARY_PROXY = "http://127.0.0.1:3456/v1"

# Breaker contract: this many consecutive empty-exhaustions pauses autonomous work.
BREAKER_THRESHOLD = 3


def _make_agent(fallback_model=None, provider="custom", base_url=PRIMARY_PROXY):
    """Minimal AIAgent mirroring tests/run_agent/test_primary_runtime_restore.py."""
    tool_defs = [{
        "type": "function",
        "function": {"name": "web_search", "description": "x",
                     "parameters": {"type": "object", "properties": {}}},
    }]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-12345678",
            base_url=base_url,
            provider=provider,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _resolve_returning(base_url):
    """resolve_provider_client mock whose client reports the requested base_url.

    Mirrors the real router: the fallback's explicit_base_url becomes the active
    client base_url, which try_activate_fallback copies onto agent.base_url.
    """
    def _side_effect(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None, **_):
        c = MagicMock()
        c.api_key = "fb-key-1234"
        c.base_url = explicit_base_url or base_url
        return (c, None)
    return _side_effect


# =============================================================================
# FIX 1 — empty-stub poison: 0 recovered chars must NOT become a length stub
# =============================================================================

class TestEmptyStubPoison:
    def test_zero_char_partial_stream_is_not_a_continuable_stub(self):
        """A partial-stream failure that recovered no text + no tool calls must be
        treated as a retryable transport error, not an empty length stub that the
        loop continues from (which is what produced the empty cascade)."""
        from agent import chat_completion_helpers as h
        decide = getattr(h, "should_return_partial_stub", None)
        assert decide is not None, (
            "FIX 1 missing: extract the stub-vs-raise decision into "
            "chat_completion_helpers.should_return_partial_stub(partial_text, "
            "partial_tool_names) so it is testable and 0-char stubs are refused."
        )
        # 0 chars, no dropped tools -> must NOT stub (raise the real error).
        assert decide("", []) is False
        assert decide(None, []) is False
        # Real recovered text -> stub is correct (user already saw it).
        assert decide("partial answer so far", []) is True
        # Dropped tool calls -> stub is correct (carries the warning).
        assert decide("", ["web_search"]) is True


# =============================================================================
# FIX 3 — consecutive empty-exhaustion circuit breaker
# =============================================================================

class TestEmptyExhaustionBreaker:
    def test_breaker_trips_after_threshold(self):
        agent = _make_agent()
        note = getattr(agent, "_note_empty_exhaustion", None)
        tripped = getattr(agent, "_empty_exhaustion_breaker_tripped", None)
        assert note and tripped, (
            "FIX 3 missing: add _note_empty_exhaustion() / "
            "_empty_exhaustion_breaker_tripped() to AIAgent."
        )
        assert agent._empty_exhaustion_breaker_tripped() is False
        for _ in range(BREAKER_THRESHOLD - 1):
            agent._note_empty_exhaustion()
        assert agent._empty_exhaustion_breaker_tripped() is False
        agent._note_empty_exhaustion()
        assert agent._empty_exhaustion_breaker_tripped() is True

    def test_successful_content_resets_breaker(self):
        agent = _make_agent()
        if not hasattr(agent, "_note_empty_exhaustion"):
            pytest.fail("FIX 3 missing: _note_empty_exhaustion")
        for _ in range(BREAKER_THRESHOLD):
            agent._note_empty_exhaustion()
        assert agent._empty_exhaustion_breaker_tripped() is True
        agent._reset_empty_exhaustion()
        assert agent._empty_exhaustion_breaker_tripped() is False


# =============================================================================
# FIX 3 — background review must not inherit a degraded fallback runtime
# =============================================================================

class TestBackgroundReviewRuntime:
    def _activate_fallback(self, agent):
        with patch("agent.auxiliary_client.resolve_provider_client",
                   side_effect=_resolve_returning(DEAD_PROXY)):
            assert agent._try_activate_fallback() is True
        assert agent._fallback_activated is True
        assert agent.base_url.startswith("http://127.0.0.1:51199")

    def test_bg_review_restores_primary_before_running(self):
        """The bg-review fork reuses the parent agent; it MUST restore the primary
        runtime before running so it never inherits the dead fallback proxy."""
        agent = _make_agent(
            fallback_model={"provider": "custom", "model": "gemini-3-flash-agent",
                            "base_url": DEAD_PROXY},
        )
        self._activate_fallback(agent)

        restored = {"called": False}
        orig = agent._restore_primary_runtime
        def _spy():
            restored["called"] = True
            return orig()
        with (
            patch.object(agent, "_restore_primary_runtime", side_effect=_spy),
            patch("run_agent.threading.Thread") as MockThread,
            patch("agent.background_review.spawn_background_review_thread",
                  return_value=(lambda: None, "prompt")),
        ):
            MockThread.return_value = MagicMock()
            agent._spawn_background_review([], review_skills=True)

        assert restored["called"], (
            "FIX 3 missing: _spawn_background_review must restore the primary "
            "runtime before spawning so bg jobs don't inherit the dead fallback."
        )

    def test_bg_review_skipped_when_breaker_tripped(self):
        """When the empty-exhaustion breaker is tripped, do not pile more autonomous
        work onto a known-dead transport — skip spawning the bg-review thread."""
        agent = _make_agent()
        if not hasattr(agent, "_note_empty_exhaustion"):
            pytest.fail("FIX 3 missing: _note_empty_exhaustion")
        for _ in range(BREAKER_THRESHOLD):
            agent._note_empty_exhaustion()

        with (
            patch("run_agent.threading.Thread") as MockThread,
            patch("agent.background_review.spawn_background_review_thread",
                  return_value=(lambda: None, "prompt")),
        ):
            agent._spawn_background_review([], review_skills=True)
            MockThread.assert_not_called()

    def test_failed_restore_preserves_proxy_marks_and_skips_background_review(self):
        agent = _make_agent(
            fallback_model={"provider": "custom", "model": "fallback", "base_url": DEAD_PROXY},
        )
        self._activate_fallback(agent)
        agent._mark_proxy_empty_exhausted(DEAD_PROXY)
        agent._rate_limited_until = float("inf")

        assert agent._restore_primary_runtime() is False
        assert DEAD_PROXY.rstrip("/").lower() in agent._empty_exhausted_base_urls

        with (
            patch.object(agent, "_restore_primary_runtime", return_value=False),
            patch("run_agent.threading.Thread") as mock_thread,
        ):
            agent._spawn_background_review([], review_skills=True)
        mock_thread.assert_not_called()


# =============================================================================
# FIX 4 — fallback must skip entries on a proxy that already empty-exhausted
# =============================================================================

class TestProxyDiverseFallback:
    def test_fallback_skips_entries_on_empty_exhausted_proxy(self):
        """Chain: two entries on the DEAD proxy, one on a healthy proxy. After the
        dead proxy is marked empty-exhausted, activation must skip both dead
        entries and land on the healthy proxy."""
        healthy = "http://127.0.0.1:3456/v1"
        agent = _make_agent(
            base_url=PRIMARY_PROXY,
            fallback_model=[
                {"provider": "custom", "model": "gemini-3-flash-agent", "base_url": DEAD_PROXY},
                {"provider": "custom", "model": "claude-sonnet-4-6", "base_url": DEAD_PROXY},
                {"provider": "custom", "model": "claude-opus-direct", "base_url": healthy},
            ],
        )
        mark = getattr(agent, "_mark_proxy_empty_exhausted", None)
        assert mark is not None, (
            "FIX 4 missing: add _mark_proxy_empty_exhausted(base_url) + skip logic "
            "in try_activate_fallback so dead-proxy entries are skipped."
        )
        agent._mark_proxy_empty_exhausted(DEAD_PROXY)

        def _resolve_per_entry(provider, model=None, raw_codex=False,
                               explicit_base_url=None, explicit_api_key=None, **_):
            c = MagicMock()
            c.api_key = "fb-key"
            c.base_url = explicit_base_url
            return (c, None)

        with patch("agent.auxiliary_client.resolve_provider_client",
                   side_effect=_resolve_per_entry):
            assert agent._try_activate_fallback() is True

        # Must have skipped the two DEAD_PROXY entries and landed on the healthy one.
        assert agent.model == "claude-opus-direct"
        assert agent.base_url.rstrip("/").endswith(":3456/v1".rstrip("/")) or \
            "3456" in agent.base_url


# =============================================================================
# Composite: the exact stall signature (pre-fix), proving the emulation reproduces
# =============================================================================

class TestStallSignatureReproduced:
    def test_single_proxy_chain_pins_to_dead_proxy_then_breaker_escapes(self):
        """End-to-end of the *logic* path: walking a single-proxy chain exhausts and
        (pre-fix) pins the agent to the dead proxy.  Post-fix, after recording the
        exhaustion and restoring, the agent is OFF the dead proxy."""
        agent = _make_agent(
            base_url=PRIMARY_PROXY,
            fallback_model=[
                {"provider": "custom", "model": "gemini-3-flash-agent", "base_url": DEAD_PROXY},
                {"provider": "custom", "model": "gemini-3.1-flash", "base_url": DEAD_PROXY},
            ],
        )
        with patch("agent.auxiliary_client.resolve_provider_client",
                   side_effect=_resolve_returning(DEAD_PROXY)):
            assert agent._try_activate_fallback() is True   # -> gemini-3-flash-agent
            assert agent._try_activate_fallback() is True   # -> gemini-3.1-flash
            assert agent._try_activate_fallback() is False  # exhausted

        # The bug: pinned to the dead proxy after exhaustion.
        assert agent.base_url.startswith("http://127.0.0.1:51199")

        # The fix contract: recording the exhaustion + restoring primary moves the
        # agent OFF the dead proxy so the next turn / bg job gets a live backend.
        if not hasattr(agent, "_note_empty_exhaustion"):
            pytest.fail("FIX 3 missing: _note_empty_exhaustion / restore-on-exhaustion")
        agent._note_empty_exhaustion()
        # Upstream #24996 arms a short (_FALLBACK_EXHAUSTED_COOLDOWN_S) cooldown on
        # chain exhaustion so the next turn does not immediately re-marshal the whole
        # context across every provider.  Assert it is armed, then expire it: the
        # stall contract is that the agent leaves the dead proxy once the cooldown
        # lapses, NOT that it is pinned there indefinitely (the 2026-06-07 failure).
        assert agent._rate_limited_until > 0, (
            "chain exhaustion must arm the upstream restore cooldown"
        )
        agent._rate_limited_until = 0
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            agent._restore_primary_runtime()
        assert agent.base_url == PRIMARY_PROXY, (
            "After empty-exhaustion the agent must restore to the primary proxy, "
            "not stay pinned to the dead fallback proxy."
        )


# =============================================================================
# Retapable replay: the recorded 2026-06-07 incident, driven through the REAL
# fallback machinery.  To re-tape, replace INCIDENT_TAPE with the model/proxy
# sequence from a future agent.log cascade and re-run.
# =============================================================================

# Recorded from agent.log session 20260607_125850_b6470b80 (13:08:22 -> 13:09:06).
INCIDENT_TAPE = {
    "session": "20260607_125850_b6470b80",
    "primary": {"model": "claude-opus-4-8", "base_url": PRIMARY_PROXY,
                "outcome": "timeout_zero_char_stub"},
    "fallbacks": [
        {"model": "gemini-3-flash-agent",     "base_url": DEAD_PROXY, "outcome": "empty"},
        {"model": "claude-sonnet-4-6",        "base_url": DEAD_PROXY, "outcome": "empty"},
        {"model": "claude-opus-4-6-thinking", "base_url": DEAD_PROXY, "outcome": "empty"},
        {"model": "gemini-3.1-flash",         "base_url": DEAD_PROXY, "outcome": "empty"},
    ],
}


class TestRecordedIncidentReplay:
    def test_replay_incident_tape_collapses_single_proxy_chain(self):
        """Replay the recorded cascade: a single-proxy chain must collapse to ONE
        activation (proxy-skip) instead of churning all four dead entries, then
        restore OFF the dead proxy for the next turn."""
        tape = INCIDENT_TAPE
        agent = _make_agent(
            base_url=tape["primary"]["base_url"],
            fallback_model=[{"provider": "custom", "model": f["model"],
                             "base_url": f["base_url"]} for f in tape["fallbacks"]],
        )

        def _resolve_per_entry(provider, model=None, raw_codex=False,
                               explicit_base_url=None, explicit_api_key=None, **_):
            c = MagicMock(); c.api_key = "k"; c.base_url = explicit_base_url
            return (c, None)

        activated = []
        with patch("agent.auxiliary_client.resolve_provider_client",
                   side_effect=_resolve_per_entry):
            # Each entry that ACTIVATES returns empty per the tape; the loop marks
            # its proxy empty-exhausted, so the walker skips the rest on that proxy.
            while agent._try_activate_fallback():
                activated.append((agent.model, agent.base_url))
                agent._mark_proxy_empty_exhausted(agent.base_url)

        assert len(activated) == 1, (
            f"Proxy-skip must collapse the single-proxy chain to one activation; "
            f"got {len(activated)}: {activated}"
        )

        agent._note_empty_exhaustion()
        # See the cooldown note in test_single_proxy_chain_pins_to_dead_proxy_...:
        # upstream #24996 gates the restore for _FALLBACK_EXHAUSTED_COOLDOWN_S.
        assert agent._rate_limited_until > 0, (
            "chain exhaustion must arm the upstream restore cooldown"
        )
        agent._rate_limited_until = 0
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            agent._restore_primary_runtime()
        assert agent.base_url == tape["primary"]["base_url"]
        assert DEAD_PROXY.rstrip("/").lower() not in agent._empty_exhausted_base_urls

    def test_replay_repeated_exhaustion_trips_breaker_and_halts_autonomy(self):
        """Replay the bg-review respin: repeated empty-exhausted turns trip the
        breaker, which halts autonomous work instead of spinning forever."""
        agent = _make_agent(
            fallback_model={"provider": "custom", "model": "gemini-3.1-flash",
                            "base_url": DEAD_PROXY},
        )
        for _ in range(BREAKER_THRESHOLD):
            agent._note_empty_exhaustion()
        assert agent._empty_exhaustion_breaker_tripped()
        with (
            patch("run_agent.threading.Thread") as MockThread,
            patch("agent.background_review.spawn_background_review_thread",
                  return_value=(lambda: None, "p")),
        ):
            agent._spawn_background_review([], review_skills=True)
            MockThread.assert_not_called()
