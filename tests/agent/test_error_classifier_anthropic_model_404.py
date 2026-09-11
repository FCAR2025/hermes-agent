"""Anthropic's `not_found_error` naming a model id is deterministic, not retryable.

Incident 2026-09-02: a session pinned to api.anthropic.com with the proxy-only
alias ``fable`` got, on every turn::

    Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error',
    'message': 'model: fable'}, 'request_id': 'req_...'}

None of ``_MODEL_NOT_FOUND_PATTERNS`` matches that wording ("model: <id>" names
no phrase like "model not found"), so the generic-404 branch classified it
``unknown`` / ``retryable=True`` and the retry loop burned three attempts on a
rejection that could never succeed before falling back.

The classifier now recognises the ``not_found_error`` + ``model: <id>`` shape.
Generic 404s with no model id (wrong endpoint path on a local llama.cpp /
Ollama / vLLM URL) must stay retryable.
"""

from agent.error_classifier import FailoverReason, classify_api_error


class MockAPIError(Exception):
    """Simulates an OpenAI/Anthropic SDK APIStatusError."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


# The exact string observed in ~/.hermes/logs/agent.log.
LIVE_404 = (
    "Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', "
    "'message': 'model: fable'}, 'request_id': 'req_011CeeNGLiZeG9t9ouU5LM3e'}"
)


def test_anthropic_model_not_found_404_is_deterministic():
    result = classify_api_error(
        MockAPIError(LIVE_404, status_code=404),
        provider="anthropic",
        model="fable",
    )
    assert result.reason == FailoverReason.model_not_found
    assert result.retryable is False
    assert result.should_fallback is True


def test_bare_404_without_model_id_stays_retryable():
    """A wrong endpoint path must not be misreported as a missing model."""
    result = classify_api_error(
        MockAPIError("404 Not Found", status_code=404),
        provider="custom",
        model="claude-fable-5-1",
    )
    assert result.reason == FailoverReason.unknown
    assert result.retryable is True


def test_not_found_error_without_model_id_stays_retryable():
    """``not_found_error`` alone (no ``model:``) is still a generic 404."""
    result = classify_api_error(
        MockAPIError(
            "Error code: 404 - {'type': 'error', 'error': "
            "{'type': 'not_found_error', 'message': 'path /v1/messages not found'}}",
            status_code=404,
        ),
        provider="custom",
        model="claude-fable-5-1",
    )
    assert result.reason == FailoverReason.unknown
    assert result.retryable is True


def test_body_naming_a_different_model_is_not_our_rejection():
    """A 404 body naming some OTHER id is about a different request (a proxy
    quoting an upstream, a wrapped error) — it must keep the generic retry
    path rather than be reported as "your model does not exist"."""
    result = classify_api_error(
        MockAPIError(LIVE_404, status_code=404),
        provider="anthropic",
        model="claude-opus-5",
    )
    assert result.reason == FailoverReason.unknown
    assert result.retryable is True


def test_model_id_match_is_case_insensitive():
    result = classify_api_error(
        MockAPIError(
            "Error code: 404 - {'type': 'error', 'error': {'type': "
            "'not_found_error', 'message': 'model: Claude-Fable-5-1'}}",
            status_code=404,
        ),
        provider="anthropic",
        model="CLAUDE-FABLE-5-1",
    )
    assert result.reason == FailoverReason.model_not_found
    assert result.retryable is False


def test_unknown_requested_model_still_classifies_on_the_body_alone():
    """When the caller passes no model there is nothing to compare against, so
    the body's own signal stands."""
    result = classify_api_error(
        MockAPIError(LIVE_404, status_code=404),
        provider="anthropic",
        model="",
    )
    assert result.reason == FailoverReason.model_not_found
    assert result.retryable is False
    assert result.should_fallback is True
