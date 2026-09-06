"""API profile scope preserves terminal Docker isolation configuration."""

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import terminal_tool


def test_named_api_profile_reads_explicit_false_isolation_flags(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "fcar-owner"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        """terminal:
  backend: docker
  docker_auto_mount_profile_files: false
  docker_network: false
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda _name: profile_home,
    )
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_NETWORK", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_AUTO_MOUNT_PROFILE_FILES", "true")

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with adapter._profile_scope("fcar-owner"):
        config = terminal_tool._get_env_config()
        container_config = terminal_tool._container_config_from_config(config)

    assert container_config["docker_auto_mount_profile_files"] is False
    assert container_config["docker_network"] is False
