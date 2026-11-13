"""Git trampoline recovery; branch updates use the real target-identity suite."""

import subprocess
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


@pytest.fixture(autouse=True)
def _isolate_venv_holders(monkeypatch):
    """The update flow's venv-holder guard sees the live gateway processes on
    a dev machine and aborts with SystemExit 2 before reaching the branch
    logic under test.  Isolate it so the test exercises the intended path."""
    monkeypatch.setattr("hermes_cli.update_cmd_windows._detect_venv_python_processes", list)


class TestGitTrampolineSelfHeal:
    """Proactive Git-for-Windows trampoline self-heal (#87876).

    A broken bin\\git.exe / cmd\\git.exe shim (~46KB) refuses every git call
    with a "BUG (fork bomb)" guard instead of re-execing the real git-core
    binary. _ensure_non_trampoline_git detects this up front and swaps in a
    real git binary when one can be located, so the normal git update path
    survives instead of degrading to the ZIP fallback.
    """

    @staticmethod
    def _fake_run_healthy(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout="git version 2.50.0.windows.1\n", stderr=""
        )

    @staticmethod
    def _fake_run_trampoline(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="BUG (fork bomb): tried to spawn itself, check your PATH\n",
        )

    @pytest.mark.platforms("windows")
    def test_healthy_git_command_unchanged(self):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_healthy,
            ),
            patch("hermes_cli.update_cmd._locate_real_git") as locate,
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        locate.assert_not_called()

    @pytest.mark.platforms("windows")
    def test_trampoline_swaps_to_real_git(self, capsys):
        from pathlib import Path

        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        real = Path(r"C:\Program Files\Git\mingw64\libexec\git-core\git.exe")
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch(
                "hermes_cli.update_cmd._locate_real_git", return_value=real
            ),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == [str(real), "-c", "windows.appendAtomically=false"]
        out = capsys.readouterr().out
        assert "switching to real git" in out

    @pytest.mark.platforms("windows")
    def test_trampoline_no_real_git_keeps_command(self, capsys):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch("hermes_cli.update_cmd._locate_real_git", return_value=None),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        out = capsys.readouterr().out
        assert "ZIP path" in out

    @pytest.mark.platforms("not windows")
    def test_off_windows_noop(self):
        from hermes_cli import update_cmd

        git_cmd = ["git"]
        with patch("hermes_cli.update_cmd.subprocess.run") as run:
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        run.assert_not_called()

    def test_portable_git_candidates_check_shared_root_first(self, tmp_path, monkeypatch):
        # Profile-scoped layout: HERMES_HOME = <root>/profiles/foo, but the
        # PortableGit tree lives under the SHARED root (monerostar review on
        # #88136). The candidate list must check get_default_hermes_root()
        # before the profile home.
        import hermes_constants
        from hermes_cli.update_cmd_git import _portable_git_candidates

        root = tmp_path / "root"
        profile_home = root / "profiles" / "foo"

        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: profile_home)

        candidates = _portable_git_candidates()
        assert candidates[0] == (
            root / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
        )
        assert candidates[1] == (
            profile_home / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
        )

    def test_probe_fork_bomb_invokes_run_git_with_project_root_cwd(self, tmp_path, monkeypatch):
        from hermes_cli import main as cli_main, update_custody
        from hermes_cli.update_cmd_git import _probe_fork_bomb

        monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)

        recorded_calls = []

        def fake_run_git(git_cmd, args, **kwargs):
            recorded_calls.append((git_cmd, args, kwargs))
            raise FileNotFoundError("fixture: git missing")

        monkeypatch.setattr(update_custody, "run_git", fake_run_git)

        def fail_subprocess_run(*args, **kwargs):
            pytest.fail("raw subprocess.run must not be called when run_git raises FileNotFoundError")

        monkeypatch.setattr("subprocess.run", fail_subprocess_run)

        result = _probe_fork_bomb(["git"])
        assert result is None
        assert len(recorded_calls) == 1
        assert recorded_calls[0][0] == ["git"]
        assert recorded_calls[0][1] == ["--version"]
        assert recorded_calls[0][2].get("cwd") == tmp_path

    @pytest.mark.parametrize(
        "exc_cls",
        [ImportError, AttributeError, TypeError, ValueError, RuntimeError],
    )
    def test_probe_fork_bomb_and_trampoline_never_raise_on_lookup_errors(
        self, monkeypatch, exc_cls
    ):
        from hermes_cli.update_cmd_git import _git_is_trampoline, _probe_fork_bomb

        def raise_lookup():
            raise exc_cls("simulated lookup failure")

        monkeypatch.setattr(update_cmd, "_m", raise_lookup)

        assert _probe_fork_bomb(["git"]) is None
        assert _git_is_trampoline(["git"]) is False

    def test_probe_fork_bomb_and_trampoline_never_raise_on_bad_project_root(
        self, monkeypatch
    ):
        from hermes_cli import main as cli_main
        from hermes_cli.update_cmd_git import _git_is_trampoline, _probe_fork_bomb

        class _BadFsPath:
            def __fspath__(self):
                raise ValueError("invalid PROJECT_ROOT fspath")

        for bad_project_root in (12345, _BadFsPath()):
            monkeypatch.setattr(cli_main, "PROJECT_ROOT", bad_project_root)
            assert _probe_fork_bomb(["git"]) is None
            assert _git_is_trampoline(["git"]) is False
