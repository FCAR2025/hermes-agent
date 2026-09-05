"""Linux systemd socket-activation contract for the API server adapter."""

import json
import os
import socket
import subprocess
import sys
import textwrap

import pytest


_CHILD = textwrap.dedent(
    r"""
    import asyncio
    import json
    import os
    import sys

    source_fd = int(sys.argv[3])
    if source_fd >= 0 and source_fd != 3:
        os.dup2(source_fd, 3)

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def read_health(host, port):
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        payload = await reader.read(8192)
        writer.close()
        await writer.wait_closed()
        return payload.decode("utf-8", "replace")

    async def main():
        mode = sys.argv[1]
        port = int(sys.argv[2])
        configured_port = port + 1 if mode == "address" else port
        fd_name = "attacker-secret-name" if mode == "name" else "fcar-runtime-api"
        os.environ["LISTEN_PID"] = str(os.getpid() + 1 if mode == "pid" else os.getpid())
        os.environ["LISTEN_FDS"] = "2" if mode == "count" else "1"
        os.environ["LISTEN_FDNAMES"] = fd_name
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={
            "host": "127.0.0.1",
            "port": configured_port,
            "key": "sk-test-strong-key-0123456789",
            "systemd_socket_activation": True,
            "systemd_fd_name": "fcar-runtime-api",
        }))
        first = await adapter.connect()
        health = ""
        second = None
        if first:
            health = await read_health("127.0.0.1", port)
            await adapter.disconnect()
            if mode == "reconnect":
                second = await adapter.connect(is_reconnect=True)
                if second:
                    health += await read_health("127.0.0.1", port)
                await adapter.disconnect()
        result = {
            "first": first,
            "second": second,
            "health_200_count": health.count("HTTP/1.1 200 OK"),
            "fatal": adapter.has_fatal_error,
            "retryable": adapter.fatal_error_retryable,
            "code": adapter.fatal_error_code,
            "message": adapter.fatal_error_message,
        }
        print(json.dumps(result, sort_keys=True))

    asyncio.run(main())
    """
)


def _tcp_socket(*, listening: bool = True) -> socket.socket:
    inherited = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    inherited.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    inherited.bind(("127.0.0.1", 0))
    if listening:
        inherited.listen(16)
    return inherited


def _run_child(mode: str, inherited: socket.socket | None) -> dict:
    pass_fds: tuple[int, ...] = ()

    if inherited is None:
        port = 9
        source_fd = -1
    else:
        source_fd = inherited.fileno()
        pass_fds = (source_fd,)
        port = inherited.getsockname()[1]

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, mode, str(port), str(source_fd)],
        cwd=os.getcwd(),
        env={**os.environ, "PYTHONPATH": os.getcwd()},
        pass_fds=pass_fds,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.linux_only
@pytest.mark.parametrize("mode", ["valid", "reconnect"])
def test_inherited_listener_serves_http_and_survives_reconnect(mode):
    with _tcp_socket() as inherited:
        result = _run_child(mode, inherited)

    assert result["first"] is True
    assert result["health_200_count"] == (2 if mode == "reconnect" else 1)
    assert result["second"] is (True if mode == "reconnect" else None)
    assert result["fatal"] is False


@pytest.mark.linux_only
@pytest.mark.parametrize("mode", ["pid", "count", "name", "address"])
def test_invalid_activation_metadata_fails_closed_without_binding(mode):
    with _tcp_socket() as inherited:
        result = _run_child(mode, inherited)

    assert result["first"] is False
    assert result["fatal"] is True
    assert result["retryable"] is False
    assert result["code"] == "api_server_systemd_socket_invalid"
    assert result["message"] == "Invalid systemd socket activation for API server."


@pytest.mark.linux_only
def test_missing_inherited_descriptor_fails_closed():
    result = _run_child("missing", None)

    assert result["first"] is False
    assert result["code"] == "api_server_systemd_socket_invalid"
    assert result["retryable"] is False


@pytest.mark.linux_only
def test_non_stream_descriptor_fails_closed():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as inherited:
        inherited.bind(("127.0.0.1", 0))
        result = _run_child("type", inherited)

    assert result["first"] is False
    assert result["code"] == "api_server_systemd_socket_invalid"
    assert result["retryable"] is False


@pytest.mark.linux_only
def test_non_listening_stream_descriptor_fails_closed():
    with _tcp_socket(listening=False) as inherited:
        result = _run_child("not-listening", inherited)

    assert result["first"] is False
    assert result["code"] == "api_server_systemd_socket_invalid"
    assert result["retryable"] is False
