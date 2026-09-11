"""Behavioral tests for Docker's automatic Hermes profile mount gate."""

import subprocess
from pathlib import Path

import pytest

import tools.terminal_tool as terminal_tool
import tools.terminal_tool_backends as terminal_backends
from tools.environments import docker as docker_env


def _capture_docker(monkeypatch):
    docker_env._cgroup_limits_ok = True
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1] == "version":
            return subprocess.CompletedProcess(cmd, 0, "Docker version", "")
        if cmd[1] == "run":
            return subprocess.CompletedProcess(cmd, 0, "container-id\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", run)
    monkeypatch.setattr(docker_env, "_egress_proxy_args_for_docker", lambda: ([], {}, []))
    return calls


def _docker_run(calls):
    return next(cmd for cmd in calls if cmd[1:3] == ["run", "-d"])


def test_disabled_profile_mounts_skip_all_helpers_and_keep_explicit_volume(monkeypatch):
    calls = _capture_docker(monkeypatch)

    def unexpected():
        pytest.fail("automatic profile mount helper must not be called")

    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", unexpected)
    monkeypatch.setattr("tools.credential_files.get_skills_directory_mount", unexpected)
    monkeypatch.setattr("tools.credential_files.get_cache_directory_mounts", unexpected)

    docker_env.DockerEnvironment(
        image="python:3.11",
        task_id="mount-gate-off",
        volumes=["/run/fcar-runtime-inputs:/inputs:ro"],
        auto_mount_profile_files=False,
        persist_across_processes=False,
    )

    run_cmd = _docker_run(calls)
    assert "/run/fcar-runtime-inputs:/inputs:ro" in run_cmd


def test_profile_mounts_remain_enabled_by_default(monkeypatch, tmp_path):
    credential = tmp_path / "credential.json"
    credential.write_text("{}", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    calls = _capture_docker(monkeypatch)
    helper_calls = []

    def credential_mounts():
        helper_calls.append("credentials")
        return [{"host_path": str(credential), "container_path": "/profile/credential.json"}]

    def skill_mounts():
        helper_calls.append("skills")
        return [{"host_path": str(skills), "container_path": "/profile/skills"}]

    def cache_mounts():
        helper_calls.append("cache")
        return [{"host_path": str(cache), "container_path": "/profile/cache"}]

    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", credential_mounts)
    monkeypatch.setattr("tools.credential_files.get_skills_directory_mount", skill_mounts)
    monkeypatch.setattr("tools.credential_files.get_cache_directory_mounts", cache_mounts)

    docker_env.DockerEnvironment(
        image="python:3.11",
        task_id="mount-gate-default",
        persist_across_processes=False,
    )

    run_cmd = _docker_run(calls)
    assert helper_calls == ["credentials", "skills", "cache"]
    assert f"{credential}:/profile/credential.json:ro" in run_cmd
    assert f"{skills}:/profile/skills:ro" in run_cmd
    assert f"{cache}:/profile/cache:ro" in run_cmd


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_constructor_rejects_non_boolean_profile_mount_option(value):
    with pytest.raises(TypeError, match="auto_mount_profile_files must be a boolean"):
        docker_env.DockerEnvironment(
            image="python:3.11",
            auto_mount_profile_files=value,
        )


def test_terminal_config_forwards_profile_mount_flag(monkeypatch):
    captured = {}

    def fake_docker(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(terminal_backends, "_DockerEnvironment", fake_docker)
    monkeypatch.setattr(terminal_tool, "_maybe_reap_docker_orphans", lambda _cc: None)

    terminal_backends._create_environment(
        env_type="docker",
        image="python:3.11",
        cwd="/root",
        timeout=60,
        container_config={"docker_auto_mount_profile_files": False},
    )

    assert captured["auto_mount_profile_files"] is False


def test_terminal_env_config_defaults_profile_mounts_on(monkeypatch):
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.delenv("TERMINAL_DOCKER_AUTO_MOUNT_PROFILE_FILES", raising=False)

    assert terminal_tool._get_env_config()["docker_auto_mount_profile_files"] is True


def test_terminal_env_config_rejects_malformed_profile_mount_value(monkeypatch):
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_AUTO_MOUNT_PROFILE_FILES", "sometimes")

    with pytest.raises(ValueError, match="TERMINAL_DOCKER_AUTO_MOUNT_PROFILE_FILES"):
        terminal_tool._get_env_config()


def test_terminal_product_config_false_overrides_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_AUTO_MOUNT_PROFILE_FILES", "true")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("terminal:\n  docker_auto_mount_profile_files: false\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.config.get_config_path", lambda: config_path)

    assert terminal_tool._get_env_config()["docker_auto_mount_profile_files"] is False


def test_terminal_product_config_rejects_non_boolean_value(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("terminal:\n  docker_auto_mount_profile_files: 'false'\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.config.get_config_path", lambda: config_path)

    with pytest.raises(
        TypeError, match="terminal.docker_auto_mount_profile_files must be a boolean"
    ):
        terminal_tool._get_env_config()


def test_existing_unreadable_config_refuses_docker_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.mkdir()
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr("hermes_cli.config.get_config_path", lambda: Path(config_path))

    with pytest.raises(RuntimeError, match="config.yaml unreadable"):
        terminal_tool._get_env_config()


@pytest.mark.parametrize(
    ("enabled", "label"),
    [(True, "hermes-profile-mounts=on"), (False, "hermes-profile-mounts=off")],
)
def test_cross_process_reuse_is_scoped_to_mount_posture(monkeypatch, enabled, label):
    calls = _capture_docker(monkeypatch)
    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", lambda: [])
    monkeypatch.setattr("tools.credential_files.get_skills_directory_mount", lambda: [])
    monkeypatch.setattr("tools.credential_files.get_cache_directory_mounts", lambda: [])

    docker_env.DockerEnvironment(
        image="python:3.11",
        task_id="mount-reuse",
        auto_mount_profile_files=enabled,
        persist_across_processes=True,
    )

    ps_cmd = next(cmd for cmd in calls if cmd[1] == "ps")
    run_cmd = _docker_run(calls)
    assert f"label={label}" in ps_cmd
    assert label in run_cmd
