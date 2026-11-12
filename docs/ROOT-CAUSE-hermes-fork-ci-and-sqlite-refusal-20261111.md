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
5. **Subprocess Encoding and Exception Ratchet Violations in Git Helpers**: Subprocess invocation in `_probe_fork_bomb` lacked explicit `encoding="utf-8"`, `errors="replace"`, and `check=False`, violating Windows footgun rules and Ruff `PLW1510`. Broad `except Exception: pass` blocks in `_probe_fork_bomb` and `_locate_real_git` violated code health ratchet rules `BLE001` and `S110`.
6. **Hardcoded Upstream Default Branch in Contributor Attribution Check**: `.github/workflows/contributor-check.yml` hardcoded `origin/main` for `git merge-base`, scanning upstream history rather than the fork PR's target branch (`github.base_ref`).
7. **Additive Schema Budget Protection Bypass during Durability Barrier Application**: In `_apply_additive_schema`, `_reapply_durability_barriers(conn)` was executed prior to arming the SQLite progress handler (`budget.arm(conn)`) and outside the transaction rollback error handler. On macOS, durability PRAGMAs (`checkpoint_fullfsync`, `synchronous=FULL`) executed on slow connections exceed the browse reconcile budget and suppress errors. Subsequent `conn.execute('BEGIN IMMEDIATE')` invoked the armed progress handler outside the `try:` block, causing `sqlite3.OperationalError: interrupted` to escape unhandled and trigger the generic corrupt-store 503 instead of the expected budget-exhaustion retryable 503.
8. **Subprocess Spawn Authority & Working Directory Drift in Git Fork Bomb Probe**: `_probe_fork_bomb` invoked `run_git` without explicitly passing `cwd`, causing the working directory to default to the process repository root rather than `update_cmd.PROJECT_ROOT`. This violated update boundary custody assertions in `test_update_target_identity.py`. Additionally, a redundant raw `subprocess.run` fallback bypassed the centralized `run_git` authority.
9. **JS/TS Checks Workflow Timeout Bounding on Fork Runners**: Hardcoded `timeout-minutes: 30` in `js-tests.yml` sized for 32-vCPU Blacksmith runners. On 2-vCPU `ubuntu-latest` fork runners at `--concurrency 2` with uncached `npm ci` and Electron setup, the job hit the 30-minute ceiling and was cancelled despite 0 errors.
10. **Managed Runtime Resolution Allowlist Omission**: `shutil.which("git")` introduced in `_locate_real_git` (`hermes_cli/update_cmd_git.py`) was not registered in `tests/fixtures/resolution_allowlist.json`, triggering AST enforcement failure in `test_bare_which_and_known_path_tables_are_allowlisted`.

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
- **Linux Python tests (1/3)**:
  - `.github/workflows/tests.yml` set `HERMES_TEST_WORKERS: ${{ github.repository == 'NousResearch/hermes-agent' && 32 || 4 }}`. On a standard 2-vCPU `ubuntu-latest` runner, 4 parallel test worker processes running multi-process daemons caused severe CPU starvation.
  - In `tests/gateway/test_journey_compress_restart_fifo.py`:
    - `ContextCompressor._find_tail_cut_by_tokens` clamps `min_tail_floor = max(3, min(self.protect_last_n, _MAX_TAIL_MESSAGE_FLOOR))` and cuts the history tail using `lean_tail_budget(64000, threshold_tokens)`.
    - Because `CompactingModel` omits `usage` on SSE streaming responses (`body.get('stream')`), `_provider_omits_usage` becomes `True` after `PRESSURE_0`, causing `_preflight_compression` to fire as soon as rough token estimates hit `threshold_tokens`.
    - In CI where system prompt lengths exceed 3.2KB, `PRESSURE_3` crossed the 12,000 token preflight threshold one turn early. With `threshold_tokens=12000`, `lean_tail_budget(64000, 12000) == 6000` (`soft_ceiling == 9000`), and `[P1, A1, P2, A2, P3]` fit inside `soft_ceiling` (`7,610 <= 9000`), so `cut=3` retained `PRESSURE_1` in the tail (`AssertionError: FOLLOWER_1`).
    - Setting `threshold_tokens: 24000` (where `lean_tail_budget == 10000`, `soft_ceiling == 15000`) and expanding the model limit guard from `50000` to `95000` bytes ensures `PRESSURE_1` always lands cleanly in the compacted middle across all system prompt sizes (0.5KB–21KB).
- **macOS OS-specific tests (1/2)**: `tests/hermes_cli/test_browse_schema_drift.py::test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out[ddl]` failed with `OwnershipConflict: Gateway runtime already owns profile .../profiles/scribe`. In `gateway/run_bootstrap.py`, `_browse_reconcile_reserve_wait_s` was `BROWSE_RECONCILE_BUDGET_S + 2.0` (5.0s total). Under contention on a 3-vCPU `macos-latest` runner, the slow browse subprocess required slightly more than 5.0s to detect timeout, abort, and clean up the flock, causing bootstrap reserve to raise `OwnershipConflict` before the lock was released.

### Failure B4: Windows footgun, Ruff PLW1510, and code health ratchet failures in `update_cmd_git.py` (run 43137784970)
- In `hermes_cli/update_cmd_git.py`, `_probe_fork_bomb` used `subprocess.run(list(argv) + ["--version"], capture_output=True, text=True, timeout=15, creationflags=windows_hide_flags())`. Because `text=True` lacked `encoding="utf-8"` and `errors="replace"`, `check-windows-footguns.py` failed with `[subprocess text=True without explicit encoding=]`. Ruff failed with `PLW1510` for missing explicit `check=False`.
- In `_probe_fork_bomb` and `_locate_real_git`, broad `except Exception: pass` was used. The code health ratchet (`scripts/code_health`) flagged these with `BLE001` (blind except Exception) and `S110` (try/except/pass).

### Failure B5: Contributor check scanning upstream commits on fork PR (run 43137784970)
- In `.github/workflows/contributor-check.yml`, `MERGE_BASE=$(git merge-base origin/main HEAD)` assumed the PR targets `origin/main`. PR #4 targets `update/joy-hermes-20261103`.
- `git merge-base origin/main HEAD` returned an upstream commit from months ago, causing `git log ${MERGE_BASE}..HEAD` to evaluate 30+ upstream commits whose authors were not in `AUTHOR_MAP` or `contributors/emails/`, causing the attribution check to fail.

### Failure B6: macOS browse schema drift timeout escapes unhandled during durability barrier execution (job 130923027575)
- In `tests/hermes_cli/test_browse_schema_drift.py::test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out[ddl]`, browse schema reconciliation runs against a `SlowConnection`.
- At `hermes_state_reconcile.py:455`, `budget = _Budget(BROWSE_RECONCILE_BUDGET_S)` starts a 3.0s deadline.
- Inside `_apply_additive_schema(path, budget)` (lines 233–238):
  - On macOS (`sys.platform == "darwin"`), `_reapply_durability_barriers(conn)` executes `PRAGMA checkpoint_fullfsync=1` and `PRAGMA synchronous=FULL` on `SlowConnection` before `budget.arm(conn)` is called, consuming the remaining 3.0s budget while suppressing internal exceptions.
  - Next, `budget.arm(conn)` arms the progress handler.
  - `conn.execute('BEGIN IMMEDIATE')` executes at line 237 OUTSIDE the `try:` block at line 238.
  - When the progress handler fires on `BEGIN IMMEDIATE`, SQLite raises `sqlite3.OperationalError: interrupted` outside `try:`, bypassing the rollback and budget translation handler at line 254 (`if budget.expired and isinstance(exc, sqlite3.OperationalError): raise BrowseBudgetExceeded(...)`).
  - Unhandled `OperationalError` escapes `reconcile_drift_for_browse` and hits `hermes_cli/web_server_sessions.py:405-419`, returning a 503 with `"Session store schema is unavailable or corrupt..."` instead of the expected declarative drift timeout error containing `"nothing was changed"` and `"sessions repair"`.

### Failure B7: Windows update target identity guard assertion failure from missing probe cwd (job 130923027692)
- In `tests/hermes_cli/test_update_target_identity.py::test_stable_zip_consumes_the_same_commit_through_the_real_swap[gitless|no-git|git-error|dirty]`, the test installs a custody guard asserting that all Git invocations have `cwd in {t.clone, t.origin}`.
- In `hermes_cli/update_cmd_git.py`, `_probe_fork_bomb(argv)` called `run_git(argv, ["--version"], timeout=15, **_GIT_TEXT_KW)` without `cwd`.
- Without `cwd`, `update_custody.run` defaulted `cwd` to `os.getcwd()` (`D:/a/hermes-agent/hermes-agent`), which is the repository root outside `{t.clone, t.origin}`, failing the custody guard assertion.
- Additionally, `_probe_fork_bomb` contained a redundant raw `subprocess.run` fallback that bypassed `update_custody.run_git`, violating single spawn authority.

### Failure B8: JS & TS checks job timeout cancellation on fork runners (job 130923026025)
- In `.github/workflows/js-tests.yml:24`, `timeout-minutes: 30` was hardcoded, assuming high-concurrency execution on `blacksmith-32vcpu-ubuntu-2404` at `--concurrency 11`.
- On 2-vCPU fork runners (`ubuntu-latest`), concurrency drops to 2 and initial environment initialization (clean `npm ci`, Electron runtime setup) consumes more wall-clock time, causing the job to hit the 30m deadline and cancel after 30 minutes with 0 errors.

### Failure B9: Managed runtime resolution allowlist omission for `_locate_real_git` (job 130923027667)
- `tests/test_managed_runtime_resolution.py::test_bare_which_and_known_path_tables_are_allowlisted` enforces that all `shutil.which` call sites are cataloged.
- In `hermes_cli/update_cmd_git.py`, `shutil.which("git")` was added in `_locate_real_git` to discover the active toolchain root without an entry in `tests/fixtures/resolution_allowlist.json`.

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
     - Configure `threshold_tokens: 24000` with `protect_last_n: 2` in test `config.yaml` and expand the context limit guard to `len(wire) > 95000` so `PRESSURE_1` lands cleanly in the compacted middle regardless of system prompt size.
     - Increase `wait_rows` timeout from 40s to 80s to accommodate 2-vCPU load spikes.
   - In `gateway/run_bootstrap.py`:
     - Set `_browse_reconcile_reserve_wait_s` to `BROWSE_RECONCILE_BUDGET_S + 7.0` (10.0s total wait) so gateway bootstrap easily outlasts slow browse reconcile teardown on 3-vCPU macOS runners.

5. **Subprocess and Exception Hygiene in Git Helpers**:
   - In `hermes_cli/update_cmd_git.py`:
     - Pass `encoding="utf-8", errors="replace", check=False` to `subprocess.run` in `_probe_fork_bomb`.
     - Narrow exceptions in `_probe_fork_bomb` to `(OSError, subprocess.SubprocessError)` and log via `logger.debug`.
     - Narrow exceptions in `_locate_real_git` to `(OSError, ValueError)` and log via `logger.debug`.
     - Ensure `_portable_git_candidates` preserves the shared and profile `root / "git" / "mingw64"` candidates first to satisfy `#88136`.

6. **Dynamic PR Base Ref in Contributor Check**:
   - In `.github/workflows/contributor-check.yml`, compute `BASE_REF="${{ github.base_ref || 'main' }}"` and resolve `MERGE_BASE=$(git merge-base "origin/${BASE_REF}" HEAD 2>/dev/null || git merge-base "${BASE_REF}" HEAD 2>/dev/null || git merge-base origin/main HEAD)`.

7. **Comprehensive Try-Block and Pre-Barrier Arming in Schema Reconcile**:
   - In `hermes_state_reconcile.py` (`_apply_additive_schema`), enclose all post-connection operations (`budget.arm(conn)`, `_reapply_durability_barriers(conn)`, `budget.check()`, `conn.execute('BEGIN IMMEDIATE')`, `apply_declared_schema`, schema read probes, and `COMMIT`) inside a single comprehensive `try / except BaseException as exc:` block.
   - Arm the progress handler via `budget.arm(conn)` before running `_reapply_durability_barriers(conn)`.
   - Call `budget.check()` immediately after `_reapply_durability_barriers(conn)` so any budget expiration tripped during barrier pragmas translates directly into `BrowseBudgetExceeded` before attempting transaction start.
   - In the exception handler, safely rollback and if `(budget.expired or budget.remaining() <= 0) and isinstance(exc, sqlite3.DatabaseError): raise BrowseBudgetExceeded(...) from exc`.

8. **Enforce Single Spawn Authority and Explicit Project Root in Fork Bomb Probe**:
   - In `hermes_cli/update_cmd_git.py`, update `_probe_fork_bomb(argv: list, cwd=None) -> Optional[bool]` to resolve `cwd` from `update_cmd._m().PROJECT_ROOT` if not explicitly provided, ensuring the probe runs within the verified project root.
   - Remove the redundant second `subprocess.run` fallback block entirely so `update_custody.run_git` remains the sole spawn authority.

9. **Dynamic Timeout for JS & TS Workflow**:
   - In `.github/workflows/js-tests.yml`, configure `timeout-minutes: ${{ github.repository == 'NousResearch/hermes-agent' && 30 || 90 }}` matching the adaptive pattern used in `tests.yml` and `tests-os.yml`.

10. **Register Executable Resolution in Allowlist**:
   - In `tests/fixtures/resolution_allowlist.json`, add an entry for `hermes_cli/update_cmd_git.py::_locate_real_git` (`bare_which`) with justification explaining Windows toolchain root discovery via PATH.

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

5. **Failure B4 Proof**:
   - `uv run --group dev python scripts/check-windows-footguns.py --all` passes with exit code 0.
   - `ruff check .` passes with exit code 0.
   - `python scripts/check --only health --base HEAD^1` passes with 0 blocking, 0 advisory violations.
   - `pytest tests/test_windows_subprocess_no_window_flags.py tests/hermes_cli/test_cmd_update.py` passes 100%.

6. **Failure B5 Proof**:
   - `.github/workflows/contributor-check.yml` parses cleanly via `yaml.safe_load`.
   - Local contributor attribution check loop against `fcar/update/joy-hermes-20261103..HEAD` passes with exit code 0.

7. **Failure B6 Proof**:
   - Dedicated unit test in `tests/hermes_cli/test_browse_schema_drift.py` verifying that when durability barrier application exceeds the budget on a slow connection, `_open_session_db_at_path` raises 503 with `"nothing was changed"` and `"sessions repair"` without corrupt store messages.
   - All tests in `tests/hermes_cli/test_browse_schema_drift.py` pass cleanly.

8. **Failure B7 Proof**:
   - Dedicated unit test in `tests/hermes_cli/test_cmd_update.py` verifying `_probe_fork_bomb` invokes `run_git` with `cwd` equal to `update_cmd.PROJECT_ROOT` and returns `None` without raw `subprocess.run` on `FileNotFoundError`.
   - All tests in `tests/hermes_cli/test_update_target_identity.py` and `tests/hermes_cli/test_cmd_update.py` pass cleanly.

9. **Failure B8 Proof**:
   - `.github/workflows/js-tests.yml` validated via `yaml.safe_load` with dynamic 90-minute timeout on fork runners.

10. **Failure B9 Proof**:
   - `pytest tests/test_managed_runtime_resolution.py` passes 8/8 tests cleanly with exit code 0.
