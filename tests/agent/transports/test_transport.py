"""Tests for the transport ABC, registry, and AnthropicTransport."""

import pytest
from types import SimpleNamespace

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse
from agent.transports import get_transport, register_transport, _REGISTRY


# ── ABC contract tests ──────────────────────────────────────────────────

class TestProviderTransportABC:
    """Verify the ABC contract is enforceable."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            ProviderTransport()

    def test_concrete_must_implement_all_abstract(self):
        class Incomplete(ProviderTransport):
            @property
            def api_mode(self):
                return "test"
        with pytest.raises(TypeError):
            Incomplete()

    def test_minimal_concrete(self):
        class Minimal(ProviderTransport):
            @property
            def api_mode(self):
                return "test_minimal"
            def convert_messages(self, messages, **kw):
                return messages
            def convert_tools(self, tools):
                return tools
            def build_kwargs(self, model, messages, tools=None, **params):
                return {"model": model, "messages": messages}
            def normalize_response(self, response, **kw):
                return NormalizedResponse(content="ok", tool_calls=None, finish_reason="stop")

        t = Minimal()
        assert t.api_mode == "test_minimal"
        assert t.validate_response(None) is True  # default
        assert t.extract_cache_stats(None) is None  # default
        assert t.map_finish_reason("end_turn") == "end_turn"  # default passthrough


# ── Registry tests ───────────────────────────────────────────────────────

class TestTransportRegistry:

    def test_get_unregistered_returns_none(self):
        assert get_transport("nonexistent_mode") is None



    def test_register_and_get(self):
        class DummyTransport(ProviderTransport):
            @property
            def api_mode(self):
                return "dummy_test"
            def convert_messages(self, messages, **kw):
                return messages
            def convert_tools(self, tools):
                return tools
            def build_kwargs(self, model, messages, tools=None, **params):
                return {}
            def normalize_response(self, response, **kw):
                return NormalizedResponse(content=None, tool_calls=None, finish_reason="stop")

        register_transport("dummy_test", DummyTransport)
        t = get_transport("dummy_test")
        assert t.api_mode == "dummy_test"
        # Cleanup
        _REGISTRY.pop("dummy_test", None)


# ── AnthropicTransport tests ────────────────────────────────────────────

class TestAnthropicTransport:

    @pytest.fixture
    def transport(self):
        import agent.transports.anthropic  # noqa: F401
        return get_transport("anthropic_messages")


    def test_convert_tools_simple(self, transport):
        tools = [{
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "A test",
                "parameters": {"type": "object", "properties": {}},
            }
        }]
        result = transport.convert_tools(tools)
        assert len(result) == 1
        assert result[0]["name"] == "test_tool"
        assert "input_schema" in result[0]







    def test_map_finish_reason(self, transport):
        assert transport.map_finish_reason("end_turn") == "stop"
        assert transport.map_finish_reason("tool_use") == "tool_calls"
        assert transport.map_finish_reason("max_tokens") == "length"
        assert transport.map_finish_reason("stop_sequence") == "stop"
        assert transport.map_finish_reason("refusal") == "content_filter"
        assert transport.map_finish_reason("model_context_window_exceeded") == "length"
        assert transport.map_finish_reason("unknown") == "stop"




    def test_normalize_response_text(self, transport):
        """Test normalization of a simple text response."""
        r = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Hello world")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            model="claude-sonnet-4-6",
        )
        nr = transport.normalize_response(r)
        assert isinstance(nr, NormalizedResponse)
        assert nr.content == "Hello world"
        assert nr.tool_calls is None or nr.tool_calls == []
        assert nr.finish_reason == "stop"

    def test_normalize_response_tool_calls(self, transport):
        """Test normalization of a tool-use response."""
        r = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_123",
                    name="terminal",
                    input={"command": "ls"},
                ),
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
            model="claude-sonnet-4-6",
        )
        nr = transport.normalize_response(r)
        assert nr.finish_reason == "tool_calls"
        assert len(nr.tool_calls) == 1
        tc = nr.tool_calls[0]
        assert tc.name == "terminal"
        assert tc.id == "toolu_123"
        assert '"command"' in tc.arguments


    # ── Regression: malformed (non-Messages) responses must not crash ──
    # A rate-limited / erroring local shim proxy (:3456 Claude-Max, :51199
    # antigravity) can return a bare string or dict error body instead of a
    # Messages object. The old code did ``for block in response.content`` and
    # raised the cryptic ``AttributeError: 'str' object has no attribute
    # 'content'`` that spammed vision / title / compression aux tasks. The
    # guard must raise a clear, catchable TypeError naming the bad payload
    # type — NEVER an AttributeError on ``.content``.

    def test_normalize_response_string_payload_raises_typeerror(self, transport):
        with pytest.raises(TypeError) as exc:
            transport.normalize_response(
                "Claude Max rate limit reached. Wait a moment and try again."
            )
        msg = str(exc.value)
        assert "str" in msg
        assert "has no attribute" not in msg  # not the cryptic AttributeError

    def test_normalize_response_dict_error_payload_raises_typeerror(self, transport):
        with pytest.raises(TypeError):
            transport.normalize_response(
                {"error": {"type": "rate_limit_error", "message": "slow down"}}
            )

    def test_normalize_response_none_raises_typeerror(self, transport):
        with pytest.raises(TypeError):
            transport.normalize_response(None)

    def test_normalize_response_content_not_a_list_raises_typeerror(self, transport):
        # `.content` present but a string (not list of blocks) — still invalid.
        r = SimpleNamespace(content="oops not a list", stop_reason="end_turn")
        with pytest.raises(TypeError):
            transport.normalize_response(r)

    def test_normalize_response_empty_list_is_safe(self, transport):
        # Empty content list is legitimate (end_turn) — must NOT raise.
        r = SimpleNamespace(content=[], stop_reason="end_turn")
        nr = transport.normalize_response(r)
        assert nr.content is None
        assert nr.finish_reason == "stop"

    # ── FIX 5: reconstruct a Messages object from a raw SSE stream string ──
    # The :3456 Claude-Max shim proxy can return the streaming SSE body to a
    # NON-streaming aux call (title / vision / compression); the SDK then passes
    # the raw text to normalize_response as a str. Reconstruct text + stop_reason
    # so the aux task succeeds instead of failing on a bare str.

    def test_normalize_response_reparses_raw_sse_stream_string(self, transport):
        sse = (
            ":ok\n\n"
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_1",'
            '"model":"claude-opus-4-8","content":[],"stop_reason":null}}\n\n'
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n\n'
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hello "}}\n\n'
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"world"}}\n\n'
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n\n'
        )
        nr = transport.normalize_response(sse)
        assert nr.content == "Hello world"
        assert nr.finish_reason == "stop"

    def test_normalize_response_plain_error_string_still_raises(self, transport):
        # A non-SSE error body (no `data:` events) must STILL raise the clear
        # TypeError — reparse only rescues genuine SSE streams, never masks a
        # real error body as empty content.
        with pytest.raises(TypeError):
            transport.normalize_response("Claude Max rate limit reached. Try again.")

    def test_build_kwargs_returns_dict(self, transport):
        """Test build_kwargs produces a usable kwargs dict."""
        messages = [{"role": "user", "content": "Hello"}]
        kw = transport.build_kwargs(
            model="claude-sonnet-4-6",
            messages=messages,
            max_tokens=1024,
        )
        assert isinstance(kw, dict)
        assert "model" in kw
        assert "max_tokens" in kw
        assert "messages" in kw

    def test_convert_messages_extracts_system(self, transport):
        """Test convert_messages separates system from messages."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = transport.convert_messages(messages)
        # System should be extracted
        assert system is not None
        # Messages should only have user
        assert len(msgs) >= 1
