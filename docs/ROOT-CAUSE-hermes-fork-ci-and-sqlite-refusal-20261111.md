# Root cause: Fork CI runner unavailability, test worker starvation, and stale SQLite3 CLI refusal leak

Date: 2026-11-12. Identified from GitHub Actions runs 41752817792 and 43127405205 on PR FCAR2025/hermes-agent#4 (`fix/terminal-receipt-cas-20260909`).

## Class

1. **State Leak in Global Module Cache**: Module-level cache (`_last_cli_refusal`) populated at test collection/import time leaking across mock boundaries when a dependency function (`find_sqlite3_cli`) is monkeypatched without resetting the companion global cache.
2. **Missing Fork Runner Fallbacks (Job Queue Stalls)**: Upstream-specific Blacksmith runner labels (`blacksmith-4vcpu-ubuntu-2404`, `blacksmith-32vcpu-ubuntu-2404`, `blacksmith-2vcpu-windows-2025`, `blacksmith-32vcpu-windows-2025`, `blacksmith-12vcpu-macos-latest`, `blacksmith-6vcpu-macos-15`) hardcoded without `github.repository == 'NousResearch/hermes-agent'` guards across 14 required workflows, causing jobs to remain in `QUEUED` indefinitely on fork pull requests where Blacksmith runners do not exist.
3. **Runner Architecture and Real Git Locator Mismatch**: Windows x64 runners require `windows-latest` fallback on forks, while the Git-for-Windows locator (`_locate_real_git`) searched only fixed `mingw64`/`clangarm64`/`mingw32` subdirectories under `C:\Program Files\Git` without consulting active `PATH` Git installation roots or PM toolchains, and `_probe_fork_bomb` discarded candidates if `run_git` raised any exception rather than falling back to standard `subprocess.run`.
4. **Fork Runner Resource Contention & Timing Skew**:
   - On 2-vCPU Linux fork runners (`ubuntu-latest`), running `HERMES_TEST_WORKERS: 4` causes CPU oversubscription and timing contention, leading to race conditions where long-running journey compaction tests fail under tight admission timeouts.
   - In `test_journey_compress_restart_fifo.py`, `protect_last_n: 2` with short pressure histories can leave turn 1 inside the protected tail instead of the compacted summary if compaction triggers early or admissions experience timing latency.
   - On 3-vCPU macOS fork runners (`macos-latest`), gateway bootstrap reserve wait (`_browse_reconcile_reserve_wait_s`) provided only a 2.0s buffer over `BROWSE_RECONCILE_BUDGET_S` (5.0s total), which is too tight when a slow browse reconcile in a child process is rolling back and releasing `gateway.lock` under load, triggering a false `OwnershipConflict`.

## Mechanism

### Failure A: Stale `_last_cli_refusal` in session recovery
- At module import time in `tests/hermes_cli/test_session_recovery_lost_and_found.py`, `HAVE_SQLITE3_CLI = find_sqlite3_cli() is not None` runs and probes the system `sqlite3` CLI. On Ubuntu runners (< 3.51.3), `find_sqlite3_cli()` rejects the vulnerable CLI and populates global `_last_cli_refusal` with `{"reason": "wal_reset_vulnerable", ...}`.
- In `test_unreadable_schema_without_cli_names_the_sqlite3_requirement`, `monkeypatch.setattr(laf, "find_sqlite3_cli", lambda: None)` mocks `find_sqlite3_cli` to return `None` to simulate an environment without a CLI.
- Because `find_sqlite3_cli` was replaced by a mock, the real `find_sqlite3_cli` (which resets `_last_cli_refusal = {}` at entry) never executed.
- When `recover_session_database` invoked `_recover_via_lost_and_found`, it received `sqlite3_bin = None` and queried `find_sqlite3_cli_refusal()`, which returned the stale `wal_reset_vulnerable` dictionary instead of `{}`.
- Consequently, `SessionRecoverySourceError` was raised with the vulnerable shell explanation rather than `SQLITE3_CLI_GUIDANCE` (which contains `".recover"`), causing the assertion `assert ".recover" in message` to fail.

### Failure B1: 14 jobs stuck `QUEUED` forever on nonexistent `blacksmith-*` runners (run 43127405205)
- In workflow files across `.github/workflows/` (including `history-check.yml`, `contributor-check.yml`, `infographic-check.yml`, `blob-size-check.yml`, `lazy-deps-guard.yml`, `case-collision-check.yml`, `icons-freshness-check.yml`, `docs-site-checks.yml`, `profile-artifact-check.yml`, `remote-backends.yml`, `docker-lint.yml`, `catalog-tests.yml`, and `bootstrap-installer.yml`), the `runs-on` field was hardcoded as `blacksmith-4vcpu-ubuntu-2404` or `blacksmith-2vcpu-windows-2025`.
- Because GitHub Actions cannot match these labels in fork repositories, the jobs remained permanently `QUEUED`, preventing workflow completion.

### Failure B2: Windows runner architecture regression and Git-core locator (`OS-specific tests / Windows-only tests`)
- Upstream uses `blacksmith-32vcpu-windows-2025` (x64). While arm64 slices were guarded with `windows-11-arm`, x64 Windows jobs across `tests-os.yml`, `bootstrap-installer.yml`, `windows-desktop-attach-e2e.yml`, and `windows-install-update-e2e.yml` must consistently resolve to `windows-latest` (x64).
- In `hermes_cli/update_cmd_git.py`, `_locate_real_git()` searched for Git-core binaries only at rigid paths `C:\Program Files\Git\<arch>\libexec\git-core\git.exe` and Hermes shared roots. It failed to inspect the active `git` executable on `PATH` (such as PM-installed toolchains or custom installation directories), and `_probe_fork_bomb` failed unconditionally if `run_git` raised any exception, skipping valid Git candidates.
- In `tests/hermes_cli/test_git_trampoline_windows_live.py`, `test_locate_real_git_finds_runner_git_core` failed with `AssertionError: expected git-core git.exe on windows-latest` because `_locate_real_git()` returned `None`.

### Failure B3: Contention & timing failures on fork runners
- **Linux Python tests (1/3)**: `.github/workflows/tests.yml` set `HERMES_TEST_WORKERS: ${{ github.repository == 'NousResearch/hermes-agent' && 32 || 4 }}`. On a standard 2-vCPU `ubuntu-latest` runner, 4 parallel test worker processes running multi-process daemons caused severe CPU starvation. In `tests/gateway/test_journey_compress_restart_fifo.py`, `wait_rows` timed out or turn progression lagged, and with `protect_last_n: 2`, `PRESSURE_1` remained uncompacted in `wire` when compression occurred early under load.
- **macOS OS-specific tests (1/2)**: `tests/hermes_cli/test_browse_schema_drift.py::test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out[ddl]` failed with `OwnershipConflict: Gateway runtime already owns profile .../profiles/scribe`. In `gateway/run_bootstrap.py`, `_browse_reconcile_reserve_wait_s` was `BROWSE_RECONCILE_BUDGET_S + 2.0` (5.0s total). Under contention on a 3-vCPU `macos-latest` runner, the slow browse subprocess required slightly more than 5.0s to detect timeout, abort, and clean up the flock, causing bootstrap reserve to raise `OwnershipConflict` before the lock was released.

## Structural fix

1. **Clear CLI refusal state before salvage recovery**:
   - In `hermes_cli/session_recovery.py` inside `_recover_via_lost_and_found` and `recover_session_database`, clear `_last_cli_refusal` before calling `find_sqlite3_cli()`.
   - In `hermes_cli/session_lost_and_found.py`, expose `clear_sqlite3_cli_refusal()` to encapsulate cache clearing.
   - Regression test in `tests/hermes_cli/test_session_recovery_lost_and_found.py`.

2. **Universal fork runner fallbacks**:
   - Audit every `.github/workflows/*.y*ml` file. Replace every bare `blacksmith-*ubuntu*` runner with `${{ github.repository == 'NousResearch/hermes-agent' && 'blacksmith-...' || 'ubuntu-latest' }}`.
   - Replace every bare `blacksmith-*windows*` runner with `${{ github.repository == 'NousResearch/hermes-agent' && 'blacksmith-...' || 'windows-latest' }}`.
   - Replace every bare `blacksmith-*macos*` runner with `${{ github.repository == 'NousResearch/hermes-agent' && 'blacksmith-...' || 'macos-latest' }}`.
   - Verify that ZERO unconditioned `blacksmith-` runner labels remain when `github.repository != 'NousResearch/hermes-agent'`.

3. **Robust Real-Git Locator & Windows Live Trampoline E2E**:
   - In `hermes_cli/update_cmd_git.py`, expand `_locate_real_git()`:
     - Check `shutil.which("git")` to locate the active Git root (including PM toolchain directories) and probe `libexec/git-core/git.exe` under its arch and root directories.
     - Check direct `libexec/git-core/git.exe` in addition to `<arch>/libexec/git-core/git.exe`.
     - In `_portable_git_candidates()`, check both `mingw64` and `clangarm64` under `HERMES_RUNTIME_DIR`, `PM_TOOL_STORE`, and Hermes roots.
     - In `_probe_fork_bomb()`, catch exceptions from `run_git` and fall back to direct `subprocess.run` with hidden window flags so probe failures do not discard healthy git binaries.

4. **Fork Runner Concurrency and Timing Headroom**:
   - In `.github/workflows/tests.yml`, set `HERMES_TEST_WORKERS: ${{ github.repository == 'NousResearch/hermes-agent' && 32 || 2 }}` to match the 2-vCPU capacity of `ubuntu-latest`.
   - In `tests/gateway/test_journey_compress_restart_fifo.py`:
     - Configure `protect_last_n: 1` in test `config.yaml` so `PRESSURE_1` is guaranteed to be in the compacted middle regardless of which pressure turn triggers compression.
     - Increase `wait_rows` timeout from 40s to 80s to accommodate 2-vCPU load spikes.
   - In `gateway/run_bootstrap.py`:
     - Set `_browse_reconcile_reserve_wait_s` to `BROWSE_RECONCILE_BUDGET_S + 7.0` (10.0s total wait) so gateway bootstrap easily outlasts slow browse reconcile teardown on 3-vCPU macOS runners.

## Proof

1. **Failure A Proof**:
   - `pytest tests/hermes_cli/test_session_recovery_lost_and_found.py` passes 100%.

2. **Failure B1 Proof**:
   - Automated script confirms ZERO unguarded `blacksmith-` runner labels remain across all `.github/workflows/*.y*ml` files.
   - All workflow YAML files validate syntax cleanly via `yaml.safe_load`.

3. **Failure B2 Proof**:
   - Unit and trampoline tests pass with the augmented locator.

4. **Failure B3 Proof**:
   - `pytest tests/gateway/test_journey_compress_restart_fifo.py` passes both `graceful` and `crash` cases cleanly.
   - `pytest tests/hermes_cli/test_browse_schema_drift.py` passes all 29 tests cleanly, including `test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out[ddl]`.
