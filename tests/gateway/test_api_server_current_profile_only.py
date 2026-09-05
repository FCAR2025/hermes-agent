"""API-server current-profile-only policy binds trusted profile context."""

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope
from gateway.config import GatewayConfig
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from gateway.session_context import (
    current_profile_only,
    get_session_env,
)
from hermes_constants import get_hermes_home, get_process_hermes_home


def test_policy_defaults_off_and_reads_canonical_config():
    with patch("hermes_cli.config.load_config", return_value={}):
        assert APIServerAdapter._resolve_current_profile_only() is False
    config = {"gateway": {"api_server": {"current_profile_only": True}}}
    with patch("hermes_cli.config.load_config", return_value=config):
        assert APIServerAdapter._resolve_current_profile_only() is True


def test_api_bind_chokepoint_sets_trusted_profile_and_policy():
    from gateway.session_context import clear_session_vars

    tokens = APIServerAdapter._bind_api_server_session(
        session_id="s1",
        profile="operator-a",
        current_profile_only=True,
    )
    try:
        assert get_session_env("HERMES_SESSION_PROFILE") == "operator-a"
        assert current_profile_only() is True
    finally:
        clear_session_vars(tokens)
    assert current_profile_only() is False


@pytest.mark.asyncio
async def test_concurrent_profile_runs_keep_home_and_session_policy_isolated(
    tmp_path, monkeypatch
):
    root = tmp_path / "hermes"
    profile_a = root / "profiles" / "operator-a"
    profile_b = root / "profiles" / "operator-b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    process_home = get_process_hermes_home()
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._current_profile_only = True
    observed = {}

    class Agent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0
        session_id = None
        _last_compaction_in_place = False

        def run_conversation(self, **_kwargs):
            profile = get_session_env("HERMES_SESSION_PROFILE")
            observed[profile] = (get_hermes_home(), current_profile_only())
            return {"final_response": profile, "messages": []}

    async def run(profile):
        token = _api_request_profile.set(profile)
        try:
            with patch.object(adapter, "_create_agent", side_effect=lambda **_kw: Agent()):
                return await adapter._run_agent(
                    user_message="test", conversation_history=[], session_id=f"s-{profile}"
                )
        finally:
            _api_request_profile.reset(token)

    await asyncio.gather(run("operator-a"), run("operator-b"))
    assert observed == {
        "operator-a": (profile_a, True),
        "operator-b": (profile_b, True),
    }
    assert get_process_hermes_home() == process_home
    assert os.environ["HERMES_HOME"] == str(root)


@pytest.mark.asyncio
async def test_structured_runs_authenticates_and_binds_each_selected_profile(
    tmp_path, monkeypatch
):
    root = tmp_path / "hermes"
    homes = {
        "operator-a": root / "profiles" / "operator-a",
        "operator-b": root / "profiles" / "operator-b",
    }
    keys = {"operator-a": "a" * 32, "operator-b": "b" * 32}
    for profile, home in homes.items():
        home.mkdir(parents=True)
        (home / ".env").write_text(
            f"API_SERVER_KEY={keys[profile]}\n", encoding="utf-8"
        )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: list(homes.items()),
    )
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", homes.__getitem__)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._current_profile_only = True
    adapter.gateway_runner = type(
        "Runner", (), {"config": GatewayConfig(multiplex_profiles=True)}
    )()
    observed = {}

    class Agent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def run_conversation(self, **_kwargs):
            profile = get_session_env("HERMES_SESSION_PROFILE")
            observed[profile] = (get_hermes_home(), current_profile_only())
            return {"final_response": profile}

    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post("/p/{profile}/v1/runs", adapter._handle_runs)
    app.router.add_get("/p/{profile}/v1/runs/{run_id}", adapter._handle_get_run)
    secret_scope.set_multiplex_active(True)
    try:
        with patch.object(adapter, "_create_agent", side_effect=lambda **_kw: Agent()):
            async with TestClient(TestServer(app)) as client:
                rejected = await client.post(
                    "/p/operator-a/v1/runs",
                    json={"input": "wrong key"},
                    headers={"Authorization": f"Bearer {keys['operator-b']}"},
                )
                assert rejected.status == 401
                starts = await asyncio.gather(*[
                    client.post(
                        f"/p/{profile}/v1/runs",
                        json={"input": profile, "session_id": f"s-{profile}"},
                        headers={"Authorization": f"Bearer {keys[profile]}"},
                    )
                    for profile in homes
                ])
                run_ids = [(await response.json())["run_id"] for response in starts]
                for profile, run_id in zip(homes, run_ids):
                    for _ in range(40):
                        response = await client.get(
                            f"/p/{profile}/v1/runs/{run_id}",
                            headers={"Authorization": f"Bearer {keys[profile]}"},
                        )
                        if (await response.json())["status"] == "completed":
                            break
                        await asyncio.sleep(0.05)
    finally:
        secret_scope.set_multiplex_active(False)

    assert observed == {
        profile: (home, True) for profile, home in homes.items()
    }
