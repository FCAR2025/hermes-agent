"""Current-profile-only API sessions keep full local session_search capability."""

import json

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session("s_local", source="api_server")
    first = store.append_message("s_local", role="user", content="local isolation marker")
    store.append_message("s_local", role="assistant", content="local result")
    store._conn.commit()
    yield store, first
    store.close()


@pytest.fixture
def isolated_profile():
    tokens = set_session_vars(
        platform="api_server",
        profile="operator-a",
        current_profile_only=True,
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize(
    ("kwargs", "mode"),
    [
        ({}, "browse"),
        ({"query": "isolation"}, "discover"),
        ({"session_id": "s_local", "profile": "operator-a"}, "read"),
    ],
)
def test_same_profile_browse_discover_and_read_remain_available(
    db, isolated_profile, kwargs, mode
):
    store, _ = db
    result = json.loads(session_search(db=store, **kwargs))
    assert result["success"] is True
    assert result["mode"] == mode


def test_same_profile_scroll_remains_available(db, isolated_profile):
    store, first = db
    result = json.loads(
        session_search(
            db=store,
            session_id="s_local",
            profile="operator-a",
            around_message_id=first,
            window=2,
        )
    )
    assert result["success"] is True
    assert result["mode"] == "scroll"


def test_explicit_foreign_profile_is_denied_before_open(monkeypatch, db, isolated_profile):
    store, _ = db
    monkeypatch.setattr(
        "tools.session_search_tool._resolve_profile_db",
        lambda _profile: pytest.fail("foreign profile DB must not open"),
    )
    result = json.loads(session_search(db=store, profile="operator-b"))
    assert result["success"] is False
    assert "current profile" in result["error"].lower()


def test_bare_id_miss_does_not_scan_sibling_profiles(monkeypatch, db, isolated_profile):
    store, _ = db
    monkeypatch.setattr(
        "tools.session_search_tool._locate_session_db",
        lambda _session_id: pytest.fail("sibling profile scan must not run"),
    )
    result = json.loads(session_search(db=store, session_id="s_foreign"))
    assert result["success"] is False
    assert result.get("profile") != "operator-b"


def test_missing_profile_scope_fails_closed_when_policy_is_enabled(db):
    store, _ = db
    tokens = set_session_vars(platform="api_server", current_profile_only=True)
    try:
        result = json.loads(session_search(db=store))
    finally:
        clear_session_vars(tokens)
    assert result["success"] is False
    assert "profile scope" in result["error"].lower()


def test_cli_cross_profile_behavior_is_unchanged(monkeypatch, db):
    store, _ = db
    other = type("OtherDB", (), {"closed": False, "list_sessions_rich": lambda self, **_kw: [], "close": lambda self: setattr(self, "closed", True)})()
    monkeypatch.setattr("tools.session_search_tool._resolve_profile_db", lambda _profile: other)
    result = json.loads(session_search(db=store, profile="operator-b"))
    assert result["success"] is True
    assert other.closed is True
