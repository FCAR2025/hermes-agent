# Testing — Hermes Agent

## Canonical Runner & Joy Memory Caps (Mandatory)

**Always run Python tests via `scripts/run_tests.sh`**, never bare `pytest`. `scripts/run_tests.sh` strips ambient credentials (`env -i`), pins `TZ=UTC`, `LANG=C.UTF-8`, and `PYTHONHASHSEED=0`, isolates `HERMES_HOME` in temp directories, and runs each test file in a fresh subprocess via `scripts/run_tests_parallel.py`.

By default, `run_tests_parallel.py` and `vitest` scale workers to `os.cpu_count()`, which can trip Joy's `earlyoom` killer (< 15% free RAM). Always wrap runs in `timeout` and pass explicit concurrency caps (`-j 2` / `--maxWorkers=2` / `-j 2 -- --test-threads=2`).

## Fastest Targeted Checks (Inner Loop)

```bash
# Single Python test file or -k filter (capped to 2 workers)
timeout 120 scripts/run_tests.sh -j 2 tests/agent/<test_file>.py -k <test_name> -q

# Blocking CI lint, AGENTS.md size caps, ruff, and code-health ratchet
timeout 120 python3 scripts/check
timeout 60 ruff check .
timeout 120 ty check --exit-zero

# Targeted TypeScript / Vitest workspace checks (capped to 2 workers)
timeout 120 npm --workspace ui-tui run typecheck
timeout 120 npx --workspace ui-tui vitest run --maxWorkers=2
timeout 120 npm --workspace web run typecheck
timeout 120 npx --workspace web vitest run --maxWorkers=2
timeout 180 npm --workspace apps/desktop run check:lint
timeout 180 npx --workspace apps/desktop vitest run --project ui --maxWorkers=2
timeout 120 npx --workspace tests-js vitest run --maxWorkers=2

# Tauri bootstrap-installer Rust unit tests (when touching apps/bootstrap-installer/src-tauri)
timeout 300 cargo test --manifest-path apps/bootstrap-installer/src-tauri/Cargo.toml --lib -j 2 -- --test-threads=2
```

## Bounded Directory Suites

Do not run the full repository suite unsharded in an interactive turn; scope to affected directories with `-j 2`:

```bash
timeout 300 scripts/run_tests.sh -j 2 tests/gateway/ tests/hermes_cli/
```

## Offline & Hermetic Fixture Rules

- **Zero live external calls in unit tests:** `@pytest.mark.integration`, `tests/e2e/`, and `tests/docker/` are excluded by default. Never hit live LLM APIs, messaging platforms, court/county portals, or real user credentials in standard tests.
- **Never touch `~/.hermes/`:** Let `tests/conftest.py` (`_isolate_hermes_home`) redirect `HERMES_HOME` to `tmp_path`. When testing profiles, mock both `Path.home` and `HERMES_HOME` (`tests/hermes_cli/test_profiles.py`).
- **JS/TS contract placement:** Tests asserting over `.ts`/`.tsx`/`package.json` belong in Vitest (`tests-js/` or workspace suites), not `tests/*.py`, so `scripts/ci/classify_changes.py` runs them on frontend-only PRs.

## TDD & Anti-Fragility Rules

- **Failing invariant test first:** Prove the bug or contract red on base with 1–2 behavior-contract tests before implementing the fix.
- **Root cause before patching:** Verify the runtime premise at the exact call site (`ROOT-CAUSE.md` discipline); patch where production binds symbols (`facade` vs defining sibling).
- **Banned test anti-patterns (`@AGENTS.md`):**
  - Never weaken, delete, or skip failing assertions.
  - Never write change-detector snapshot tests (e.g., hardcoding catalog lengths or `_config_version` literals).
  - Never read `.py`/`.ts`/`.tsx` source text in tests to regex-match implementation shape.
  - Never fake `sys.platform`; mark OS-specific tests with `@pytest.mark.linux_only`, `macos_only`, or `windows_only` (never bare `skipif`).
