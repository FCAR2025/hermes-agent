"""Profile isolation survives AIAgent's sequential and parallel dispatch."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_state import SessionDB
from run_agent import AIAgent


def _tool_definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "session_search",
                "description": "Search prior sessions",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _tool_call(call_id: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name="session_search",
            arguments=json.dumps(arguments),
        ),
    )


@pytest.fixture
def agent_with_history(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    db = SessionDB(tmp_path / "state.db")
    db.create_session("current-session", source="api_server")
    db.create_session("own-history", source="api_server")
    db.set_session_title("own-history", "Own profile history")
    db.append_message("own-history", role="user", content="own profile marker")
    db._conn.commit()

    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_definitions()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id="current-session",
            platform="api_server",
        )
    agent.client = MagicMock()

    try:
        yield agent
    finally:
        db.close()


def _run_in_isolated_profile(agent: AIAgent, calls: list[SimpleNamespace]) -> list[dict]:
    messages: list[dict] = []
    tokens = set_session_vars(
        platform="api_server",
        profile="operator-a",
        current_profile_only=True,
    )
    try:
        with patch(
            "tools.session_search_tool._resolve_profile_db",
            side_effect=AssertionError("foreign profile database must not open"),
        ):
            agent._execute_tool_calls(
                SimpleNamespace(content="", tool_calls=calls),
                messages,
                "profile-isolation-task",
            )
    finally:
        clear_session_vars(tokens)
    return messages


def test_single_session_search_denies_foreign_profile_before_database_open(
    agent_with_history,
):
    messages = _run_in_isolated_profile(
        agent_with_history,
        [_tool_call("foreign", {"profile": "operator-b", "query": "needle"})],
    )

    result = json.loads(messages[0]["content"])
    assert result["success"] is False
    assert "current profile" in result["error"].lower()


def test_parallel_session_search_denies_foreign_and_browses_own_profile(
    agent_with_history,
):
    messages = _run_in_isolated_profile(
        agent_with_history,
        [
            _tool_call(
                "foreign",
                {"profile": "operator-b", "query": "profile isolation"},
            ),
            _tool_call("own", {}),
        ],
    )

    assert [message["tool_call_id"] for message in messages] == ["foreign", "own"]
    foreign = json.loads(messages[0]["content"])
    own = json.loads(messages[1]["content"])
    assert foreign["success"] is False
    assert "current profile" in foreign["error"].lower()
    assert own["success"] is True
    assert own["mode"] == "browse"
    assert [(row["title"], row["link"]) for row in own["results"]] == [
        ("Own profile history", "@session:operator-a/own-history")
    ]
