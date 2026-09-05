"""End-to-end authorization tests for plugin-scoped dashboard sessions."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


class _ScopedStubProvider(StubAuthProvider):
    name = "scoped-stub"

    def __init__(self, surface: str = "plugin:fcar-command"):
        super().__init__()
        self.surface = surface

    def complete_login(self, **kwargs):
        return replace(super().complete_login(**kwargs), surface=self.surface)

    def verify_session(self, *, access_token: str):
        session = super().verify_session(access_token=access_token)
        return replace(session, surface=self.surface) if session else None

    def refresh_session(self, *, refresh_token: str):
        return replace(
            super().refresh_session(refresh_token=refresh_token),
            surface=self.surface,
        )


_FCAR_MANIFEST = {
    "name": "fcar-command",
    "label": "FCAR Command",
    "description": "Scoped command dashboard",
    "icon": "Shield",
    "version": "1.0.0",
    "tab": {"path": "/fcar-command", "position": "end"},
    "entry": "dist/index.js",
    "css": "dist/style.css",
    "has_api": True,
    "source": "bundled",
    "_dir": "/nonexistent/fcar-command/dashboard",
    "_api_file": "plugin_api.py",
}


@pytest.fixture
def scoped_client(monkeypatch):
    clear_providers()
    register_provider(_ScopedStubProvider())
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
        "resolver": getattr(
            web_server.app.state, "dashboard_plugin_resolver", None
        ),
    }
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    web_server.app.state.dashboard_plugin_resolver = (
        lambda plugin_id: _FCAR_MANIFEST if plugin_id == "fcar-command" else None
    )
    monkeypatch.setattr(
        web_server,
        "_get_dashboard_plugins",
        lambda force_rescan=False: [
            dict(_FCAR_MANIFEST),
            {
                **_FCAR_MANIFEST,
                "name": "other-plugin",
                "label": "Other Plugin",
                "tab": {"path": "/other-plugin", "position": "end"},
            },
        ],
    )
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    _login(client, "scoped-stub")
    yield client
    clear_providers()
    web_server.app.state.bound_host = previous["bound_host"]
    web_server.app.state.bound_port = previous["bound_port"]
    web_server.app.state.auth_required = previous["auth_required"]
    if previous["resolver"] is None:
        try:
            del web_server.app.state.dashboard_plugin_resolver
        except AttributeError:
            pass
    else:
        web_server.app.state.dashboard_plugin_resolver = previous["resolver"]


def _login(client: TestClient, provider: str) -> None:
    start = client.get(
        f"/auth/login?provider={provider}", follow_redirects=False
    )
    assert start.status_code == 302
    state = start.headers["location"].split("state=")[1]
    callback = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 302


def test_scoped_session_can_only_boot_its_registered_plugin(scoped_client):
    for path in (
        "/",
        "/fcar-command",
        "/fcar-command/",
        "/api/auth/me",
        "/api/dashboard/plugins",
        "/api/plugins/fcar-command",
        "/api/plugins/fcar-command/status",
        "/dashboard-plugins/fcar-command/dist/index.js",
        "/assets/app.js",
        "/favicon.ico",
    ):
        response = scoped_client.get(path, follow_redirects=False)
        assert response.status_code != 403, (path, response.text)

    me = scoped_client.get("/api/auth/me")
    assert me.json()["surface"] == "plugin:fcar-command"

    manifests = scoped_client.get("/api/dashboard/plugins")
    assert manifests.status_code == 200
    assert [manifest["name"] for manifest in manifests.json()] == ["fcar-command"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/sessions"),
        ("GET", "/chat"),
        ("GET", "/plugins"),
        ("GET", "/config"),
        ("GET", "/env"),
        ("GET", "/api/sessions"),
        ("GET", "/api/config"),
        ("GET", "/api/env"),
        ("GET", "/api/dashboard/plugins/hub"),
        ("GET", "/api/plugins/other-plugin/status"),
        ("GET", "/dashboard-plugins/other-plugin/dist/index.js"),
        ("POST", "/api/auth/ws-ticket"),
        ("POST", "/api/gateway/restart"),
        ("POST", "/api/cron/jobs"),
    ],
)
def test_scoped_session_denies_admin_and_other_plugin_paths(
    scoped_client, method, path
):
    response = scoped_client.request(method, path, follow_redirects=False)
    assert response.status_code == 403, (path, response.status_code, response.text)
    assert response.json()["error"] == "restricted_surface"


@pytest.mark.parametrize(
    "path",
    [
        "/api/plugins/fcar-command/../other-plugin/secret",
        "/api/plugins/fcar-command/%2e%2e/other-plugin/secret",
        "/api/plugins/fcar-command%2f..%2fother-plugin/secret",
        "/dashboard-plugins/fcar-command/%2e%2e/other-plugin/dist/index.js",
        "/dashboard-plugins/fcar-command\\..\\other-plugin/dist/index.js",
        "//api/plugins/fcar-command/status",
    ],
)
def test_scoped_session_rejects_path_variants_and_traversal(scoped_client, path):
    response = scoped_client.get(path, follow_redirects=False)
    assert response.status_code == 403, (path, response.status_code, response.text)


def test_scoped_session_cannot_mint_or_use_dashboard_websocket_ticket(
    scoped_client,
):
    ticket = scoped_client.post("/api/auth/ws-ticket")
    assert ticket.status_code == 403

    with pytest.raises(WebSocketDisconnect) as exc:
        with scoped_client.websocket_connect("/api/ws"):
            pass
    assert exc.value.code == 4401


@pytest.mark.parametrize(
    "surface",
    ["plugin:", "plugin:FCAR", "plugin:fcar/command", "admin", "dashboard:admin"],
)
def test_unknown_or_forged_surface_fails_closed(monkeypatch, surface):
    clear_providers()
    register_provider(_ScopedStubProvider(surface))
    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    previous_resolver = getattr(
        web_server.app.state, "dashboard_plugin_resolver", None
    )
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.dashboard_plugin_resolver = lambda _plugin_id: _FCAR_MANIFEST
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    _login(client, "scoped-stub")

    response = client.get("/api/auth/me")
    assert response.status_code == 403
    assert response.json()["error"] == "invalid_session_surface"

    clear_providers()
    web_server.app.state.auth_required = previous_required
    web_server.app.state.bound_host = previous_host
    web_server.app.state.dashboard_plugin_resolver = previous_resolver


def test_unregistered_plugin_surface_fails_closed(monkeypatch):
    clear_providers()
    register_provider(_ScopedStubProvider("plugin:not-installed"))
    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    previous_resolver = getattr(
        web_server.app.state, "dashboard_plugin_resolver", None
    )
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.dashboard_plugin_resolver = lambda _plugin_id: None
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    _login(client, "scoped-stub")

    response = client.get("/api/auth/me")
    assert response.status_code == 403
    assert response.json()["error"] == "invalid_session_surface"

    clear_providers()
    web_server.app.state.auth_required = previous_required
    web_server.app.state.bound_host = previous_host
    web_server.app.state.dashboard_plugin_resolver = previous_resolver


@pytest.mark.parametrize(
    "registered_path", ["/api/config", "/../config", "//config"]
)
def test_registered_plugin_with_unsafe_page_path_fails_closed(registered_path):
    clear_providers()
    register_provider(_ScopedStubProvider())
    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    previous_resolver = getattr(
        web_server.app.state, "dashboard_plugin_resolver", None
    )
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.dashboard_plugin_resolver = lambda _plugin_id: {
        **_FCAR_MANIFEST,
        "tab": {"path": registered_path},
    }
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    _login(client, "scoped-stub")

    response = client.get("/api/auth/me")
    assert response.status_code == 403
    assert response.json()["error"] == "invalid_session_surface"

    clear_providers()
    web_server.app.state.auth_required = previous_required
    web_server.app.state.bound_host = previous_host
    web_server.app.state.dashboard_plugin_resolver = previous_resolver


def test_plugin_registration_resolution_error_fails_closed():
    clear_providers()
    register_provider(_ScopedStubProvider())
    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    previous_resolver = getattr(
        web_server.app.state, "dashboard_plugin_resolver", None
    )
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "fly-app.fly.dev"

    def _broken_resolver(_plugin_id):
        raise RuntimeError("discovery unavailable")

    web_server.app.state.dashboard_plugin_resolver = _broken_resolver
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    _login(client, "scoped-stub")

    response = client.get("/api/auth/me")
    assert response.status_code == 403
    assert response.json()["error"] == "invalid_session_surface"

    clear_providers()
    web_server.app.state.auth_required = previous_required
    web_server.app.state.bound_host = previous_host
    web_server.app.state.dashboard_plugin_resolver = previous_resolver


def test_default_dashboard_session_retains_full_authority(gated_app):
    _login(gated_app, "stub")

    assert gated_app.get("/api/auth/me").json()["surface"] == "dashboard"
    assert gated_app.get("/api/config").status_code != 403
    assert gated_app.post("/api/auth/ws-ticket").status_code == 200


@pytest.fixture
def gated_app():
    clear_providers()
    register_provider(StubAuthProvider())
    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "fly-app.fly.dev"
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.auth_required = previous_required
    web_server.app.state.bound_host = previous_host
