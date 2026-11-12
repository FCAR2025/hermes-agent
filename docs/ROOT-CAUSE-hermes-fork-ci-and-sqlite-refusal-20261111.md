# Root cause: Fork CI runner unavailability, test worker starvation, and stale SQLite3 CLI refusal leak

Date: 2026-11-12. Identified from GitHub Actions run 41752817792 on PR FCAR2025/hermes-agent#4 (`fix/terminal-receipt-cas-20260909`).

## Class

1. **State Leak in Global Module Cache**: Module-level cache (`_last_cli_refusal`) populated at test collection/import time leaking across mock boundaries when a dependency function (`find_sqlite3_cli`) is monkeypatched without resetting the companion global cache.
2. **Fork Environment Resource Mismatch**: GitHub Actions workflow configurations assuming upstream-only custom runners (`windows-latest-32-arm-core`, `blacksmith-32vcpu-windows-2025`) and 32-core runner concurrency parameters (`HERMES_TEST_WORKERS: 16`), causing queued job cancellation on missing runner labels and CPU starvation timeouts on standard 4-vCPU Windows and 3-vCPU macOS fork runners.

## Mechanism

### Failure A: Stale `_last_cli_refusal` in session recovery
- At module import time in `tests/hermes_cli/test_session_recovery_lost_and_found.py`, `HAVE_SQLITE3_CLI = find_sqlite3_cli() is not None` runs and probes the system `sqlite3` CLI. On Ubuntu runners (< 3.51.3), `find_sqlite3_cli()` rejects the vulnerable CLI and populates global `_last_cli_refusal` with `{"reason": "wal_reset_vulnerable", ...}`.
- In `test_unreadable_schema_without_cli_names_the_sqlite3_requirement`, `monkeypatch.setattr(laf, "find_sqlite3_cli", lambda: None)` mocks `find_sqlite3_cli` to return `None` to simulate an environment without a CLI.
- Because `find_sqlite3_cli` was replaced by a mock, the real `find_sqlite3_cli` (which resets `_last_cli_refusal = {}` at entry) never executed.
- When `recover_session_database` invoked `_recover_via_lost_and_found`, it received `sqlite3_bin = None` and queried `find_sqlite3_cli_refusal()`, which returned the stale `wal_reset_vulnerable` dictionary instead of `{}`.
- Consequently, `SessionRecoverySourceError` was raised with the vulnerable shell explanation rather than `SQLITE3_CLI_GUIDANCE` (which contains `".recover"`), causing the assertion `assert ".recover" in message` to fail.

### Failure B1: Non-existent `windows-latest-32-arm-core` runner in forks
- In `.github/workflows/tests-os.yml` (lines 98-107) and `.github/workflows/pm-bundle.yml` (line 80), the `win32-arm64` jobs requested runner `windows-latest-32-arm-core`.
- This custom runner label exists only in the upstream repository (`NousResearch/hermes-agent`). In forks (`FCAR2025/hermes-agent`), GitHub Actions cannot route the job, leaving it queued until cancelled, which fails the required checks suite.

### Failure B2: Fork runner CPU starvation and timeouts
- In `.github/workflows/tests-os.yml` line 212, `HERMES_TEST_WORKERS` was hardcoded to `'16'` on Windows, appropriate for 32-vCPU Blacksmith runners but overwhelming standard 4-vCPU `windows-latest` runners into 300s job timeouts. Similarly, on 3-vCPU macOS runners, excessive worker load starved subprocesses (such as `test_browse_schema_drift.py`'s 5s subprocess budget).
- The default 30-minute job timeout and 300s file timeout were too tight for fork runners operating under constrained concurrency.

## Structural fix

1. **Clear CLI refusal state before salvage recovery**:
   - In `hermes_cli/session_recovery.py` inside `_recover_via_lost_and_found` (and `recover_session_database`), clear `_last_cli_refusal` before calling `find_sqlite3_cli()`.
   - In `hermes_cli/session_lost_and_found.py`, expose `clear_sqlite3_cli_refusal()` to encapsulate cache clearing.
   - Add a regression test verifying that pre-seeded `_last_cli_refusal = {"reason": "wal_reset_vulnerable", "detail": "stale"}` does not prevent emission of `SQLITE3_CLI_GUIDANCE` when `find_sqlite3_cli` returns `None`.

2. **Conditional runner fallbacks for fork repositories**:
   - In `.github/workflows/tests-os.yml` (lines 98-107), set `runner: ${{ github.repository == 'NousResearch/hermes-agent' && 'windows-latest-32-arm-core' || 'windows-11-arm' }}` for Windows arm64 slices 1/2 and 2/2.
   - In `.github/workflows/pm-bundle.yml` (line 80), set `runner: ${{ github.repository == 'NousResearch/hermes-agent' && 'windows-latest-32-arm-core' || 'windows-11-arm' }}` for `win32-arm64`.

3. **Bound test workers and adjust timeouts for fork runners**:
   - In `.github/workflows/tests-os.yml`:
     - Set `HERMES_TEST_WORKERS: ${{ github.repository != 'NousResearch/hermes-agent' && '2' || (matrix.marker == 'windows' && '16' || '') }}`.
     - Set `HERMES_TEST_FILE_TIMEOUT: ${{ github.repository == 'NousResearch/hermes-agent' && '300' || '600' }}`.
     - Set `timeout-minutes: ${{ matrix.timeout || (github.repository == 'NousResearch/hermes-agent' && 30 || 60) }}`.

## Proof

1. **Failure A Proof**:
   - Before fix: `test_unreadable_schema_without_cli_names_the_sqlite3_requirement` and `test_stale_cli_refusal_cleared_before_salvage` failed with `AssertionError: assert '.recover' in '...: it stale...'`.
   - After fix: both tests pass cleanly in `tests/hermes_cli/test_session_recovery_lost_and_found.py`.

2. **Failure B Proof**:
   - Targeted tests in `tests/ci/` (`test_clean_worktree_check.py`, `test_required_results.py`, `test_classify_changes.py`, `test_stable_release_graph.py`, `test_stable_release_commit_threading.py`, `test_list_os_marked_tests.py`) validate workflow contracts and pass with 0 errors.
