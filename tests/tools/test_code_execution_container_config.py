"""Container isolation config propagation for execute_code."""

import threading
from unittest.mock import patch

import tools.code_execution_tool as code_execution_tool


def test_execute_code_forwards_fcar_isolation_flags():
    captured = {}

    class FakeEnvironment:
        pass

    def create_environment(**kwargs):
        captured.update(kwargs)
        return FakeEnvironment()

    config = {
        "env_type": "docker",
        "docker_image": "python:3.11",
        "cwd": "/root",
        "timeout": 180,
        "docker_auto_mount_profile_files": False,
        "docker_network": False,
    }
    with (
        patch("tools.terminal_tool._get_env_config", return_value=config),
        patch("tools.terminal_tool._task_env_overrides", {}),
        patch("tools.terminal_tool._active_environments", {}),
        patch("tools.terminal_tool._creation_locks", {}),
        patch("tools.terminal_tool._creation_locks_lock", threading.Lock()),
        patch("tools.terminal_tool._env_lock", threading.Lock()),
        patch("tools.terminal_tool._create_environment", side_effect=create_environment),
        patch("tools.terminal_tool._start_cleanup_thread"),
    ):
        environment, env_type = code_execution_tool._get_or_create_env("fcar-api-task")

    assert isinstance(environment, FakeEnvironment)
    assert env_type == "docker"
    assert captured["container_config"]["docker_auto_mount_profile_files"] is False
    assert captured["container_config"]["docker_network"] is False
