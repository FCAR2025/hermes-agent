---
name: hermes-verify
description: Use when verifying Python, TypeScript, or Rust installer changes in hermes-agent before handoff or pull request submission.
---

# Hermes Agent Verification Protocol

Run these bounded, concurrency-capped commands from your active `hermes-agent` worktree so Joy's `earlyoom` (< 15% free RAM) never triggers.

## 1. Branch & Hygiene Check
- Confirm worktree branch state and verify `.lease.json` or untracked scratch artifacts are not staged:
  ```bash
  timeout 15 git status --short --branch
  ```
- Never read `.env` / `~/.hermes/.env` or run bare `pytest`.

## 2. Python Lint & Blocking CI Guards (`scripts/check`)
```bash
timeout 120 python3 scripts/check
timeout 30 python3 scripts/ci/check_agents_md_size.py
```

## 3. Bounded Python Tests (`scripts/run_tests.sh -j 2`)
Run only the touched test files/directories in hermetic subprocesses capped to 2 workers:
```bash
timeout 180 scripts/run_tests.sh -j 2 tests/<area>/test_<target>.py
```

## 4. Scoped TypeScript / Rust Checks (When Applicable)
- **TUI / Web / Root JS:**
  ```bash
  timeout 120 npm --workspace ui-tui run typecheck && timeout 120 npx --workspace ui-tui vitest run --maxWorkers=2
  timeout 120 npm --workspace web run typecheck && timeout 120 npx --workspace web vitest run --maxWorkers=2
  ```
- **Desktop (`apps/desktop`):**
  ```bash
  timeout 180 npm --workspace apps/desktop run check:lint
  timeout 180 npx --workspace apps/desktop vitest run --project ui --maxWorkers=2
  ```
- **Bootstrap Installer (`apps/bootstrap-installer/src-tauri`):**
  ```bash
  timeout 300 cargo test --manifest-path apps/bootstrap-installer/src-tauri/Cargo.toml --lib -j 2 -- --test-threads=2
  ```
Quote fresh command output and exit codes in your handoff before claiming completion.
