"""Native run approval polling and exact-resolution contracts."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import approval as approval_mod


def _make_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_post(
        "/v1/runs/{run_id}/approval", adapter._handle_run_approval
    )
    return app


def _entry(request_id: str, command: str, description: str, **extra):
    return approval_mod._ApprovalEntry(
        {
            "request_id": request_id,
            "command": command,
            "description": description,
            "pattern_keys": ["shell-c"],
            **extra,
        }
    )


@pytest.fixture(autouse=True)
def _isolated_gateway_approval_queues():
    with approval_mod._lock:
        approval_mod._gateway_queues.clear()
    yield
    with approval_mod._lock:
        entries = [
            entry
            for queue in approval_mod._gateway_queues.values()
            for entry in queue
        ]
        approval_mod._gateway_queues.clear()
    for entry in entries:
        entry.event.set()


@pytest.fixture
def adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _register_run(adapter: APIServerAdapter, run_id: str) -> None:
    adapter._set_run_status(run_id, "running", session_id=f"session-{run_id}")
    adapter._run_approval_sessions[run_id] = run_id


@pytest.mark.asyncio
async def test_poll_restores_bounded_redacted_pending_approval_without_sse(adapter):
    run_id = "run_poll"
    request_id = "1" * 32
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
    _register_run(adapter, run_id)
    pending = _entry(
        request_id,
        f"OPENAI_API_KEY={secret} curl https://example.test",
        f"Use credential {secret}",
        smart_denied=True,
        allow_permanent=False,
        arbitrary_secret=secret,
    )
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = [pending]

    # Model a disconnected/destroyed SSE transport: polling must use the
    # authoritative approval queue, not replay state from the stream.
    adapter._run_streams.pop(run_id, None)
    adapter._run_streams_created.pop(run_id, None)

    async with TestClient(TestServer(_make_app(adapter))) as client:
        response = await client.get(f"/v1/runs/{run_id}")
        payload = await response.json()

    assert response.status == 200
    assert payload["status"] == "waiting_for_approval"
    approval = payload["pending_approval"]
    assert set(approval) == {
        "request_id",
        "command",
        "description",
        "smart_denied",
        "choices",
    }
    assert approval["request_id"] == request_id
    assert approval["choices"] == ["once", "deny"]
    assert secret not in approval["command"]
    assert secret not in approval["description"]
    assert len(approval["command"]) <= 4096
    assert len(approval["description"]) <= 1024


@pytest.mark.asyncio
async def test_exact_second_approval_leaves_first_waiting(adapter):
    run_id = "run_parallel"
    first = _entry("1" * 32, "first command", "first")
    second = _entry("2" * 32, "second command", "second")
    _register_run(adapter, run_id)
    adapter._run_streams[run_id] = asyncio.Queue()
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = [first, second]

    async with TestClient(TestServer(_make_app(adapter))) as client:
        response = await client.post(
            f"/v1/runs/{run_id}/approval",
            json={"choice": "once", "request_id": "2" * 32},
        )
        polled = await client.get(f"/v1/runs/{run_id}")
        status = await polled.json()

    assert response.status == 200
    assert second.event.is_set()
    assert second.result == "once"
    assert not first.event.is_set()
    assert first.result is None
    assert status["status"] == "waiting_for_approval"
    assert status["pending_approval"]["request_id"] == "1" * 32


@pytest.mark.asyncio
async def test_stale_or_wrong_run_request_id_never_falls_back_to_oldest(adapter):
    target_run = "run_target"
    wrong_run = "run_wrong"
    first = _entry("1" * 32, "first command", "first")
    second = _entry("2" * 32, "second command", "second")
    _register_run(adapter, target_run)
    _register_run(adapter, wrong_run)
    with approval_mod._lock:
        approval_mod._gateway_queues[target_run] = [first, second]

    async with TestClient(TestServer(_make_app(adapter))) as client:
        stale = await client.post(
            f"/v1/runs/{target_run}/approval",
            json={"choice": "once", "request_id": "f" * 32},
        )
        wrong = await client.post(
            f"/v1/runs/{wrong_run}/approval",
            json={"choice": "once", "request_id": "2" * 32},
        )

    assert stale.status == 409
    assert wrong.status == 409
    assert not first.event.is_set()
    assert not second.event.is_set()
    with approval_mod._lock:
        assert approval_mod._gateway_queues[target_run] == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_id",
    [None, "", "ABCDEF0123456789ABCDEF0123456789", "x" * 32, 42],
)
async def test_request_id_must_be_canonical_uuid_hex(adapter, request_id):
    run_id = "run_invalid_id"
    pending = _entry("1" * 32, "command", "description")
    _register_run(adapter, run_id)
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = [pending]

    async with TestClient(TestServer(_make_app(adapter))) as client:
        response = await client.post(
            f"/v1/runs/{run_id}/approval",
            json={"choice": "once", "request_id": request_id},
        )

    assert response.status == 400
    assert not pending.event.is_set()


@pytest.mark.asyncio
async def test_request_id_cannot_be_combined_with_resolve_all(adapter):
    run_id = "run_conflict"
    pending = _entry("1" * 32, "command", "description")
    _register_run(adapter, run_id)
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = [pending]

    async with TestClient(TestServer(_make_app(adapter))) as client:
        response = await client.post(
            f"/v1/runs/{run_id}/approval",
            json={
                "choice": "once",
                "request_id": "1" * 32,
                "resolve_all": True,
            },
        )

    assert response.status == 400
    assert not pending.event.is_set()


@pytest.mark.asyncio
async def test_legacy_once_and_deny_still_resolve_fifo(adapter):
    run_id = "run_legacy"
    first = _entry("1" * 32, "first command", "first")
    second = _entry("2" * 32, "second command", "second")
    _register_run(adapter, run_id)
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = [first, second]

    async with TestClient(TestServer(_make_app(adapter))) as client:
        once = await client.post(
            f"/v1/runs/{run_id}/approval", json={"choice": "once"}
        )
        deny = await client.post(
            f"/v1/runs/{run_id}/approval", json={"choice": "deny"}
        )
        final = await client.get(f"/v1/runs/{run_id}")
        final_status = await final.json()

    assert once.status == 200
    assert deny.status == 200
    assert first.result == "once"
    assert second.result == "deny"
    assert final_status["status"] == "running"
    assert "pending_approval" not in final_status
