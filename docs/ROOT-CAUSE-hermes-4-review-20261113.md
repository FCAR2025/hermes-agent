# Root cause: Unconditioned empty-completion gate skip, narrowed fork-bomb probe exception contract, and unverifiable PR body claims

Date: 2026-11-13. Identified from `muse` review on PR `FCAR2025/hermes-agent#4` (`fix/terminal-receipt-cas-20260909` at `1339ce0ce72aca8c8073ebe2fd17aab9166ad873`).

Threat model and scope: In scope are library and CLI callers invoking `complete_task(..., require_blocked_unclaimed=True)` across `local-only`/unset and PR-governed `completion_contract` cards, updater git-trampoline probes encountering module/attribute/path/subprocess failures during `hermes update`, and static packet verification of `PR_BODY.md` against `DIFF.patch` and changed files. Out of scope are hostile local processes mutating SQLite tables directly outside `kanban_db`.

## Class

1. **Unconditioned Gate Exemption Across Orthogonal Modes (`hermes_cli/kanban_db.py`)**: Conditioning the `_gate_empty_completion` skip solely on the transport CAS flag (`require_blocked_unclaimed`) instead of the presence of a compensating completion contract (`acceptance is not None`) exempted cards with `completion_contract` unset or `"local-only"` from all completion-evidence checks, allowing a blocked unclaimed card to transition to `done` with `NULL` result and no synthesized run.
2. **Broken Total-Function Exception Boundary (`hermes_cli/update_cmd_git.py`)**: Narrowing `_probe_fork_bomb`'s catch-all `except Exception` to `(OSError, subprocess.SubprocessError)` while resolving `_m().PROJECT_ROOT`, `Path(root).is_dir()`, and `run_git(...)` inside the `try` block violated `_git_is_trampoline`'s documented `"Never raises"` total-function contract whenever import, attribute, type, value, or runtime errors occur.
3. **Out-of-Packet Claims in Review Metadata (`PR_BODY.md`)**: Referencing historical commit SHAs, external lane notes, and test execution counts from untouched test suites in `PR_BODY.md` produced claims that cannot be verified from `DIFF.patch` and the changed files included in the review packet.

## Mechanism

1. **Finding 1 — `complete_task` empty-completion bypass (`hermes_cli/kanban_db.py`)**:
   - `complete_task` guarded `_gate_empty_completion(conn, task_id, result=result, summary=summary)` with `if not require_blocked_unclaimed:` so a PR-governed terminal receipt (`completion_contract="acme/repo"`, `metadata={"published_pr": ...}`, no prose `result`/`summary`) could complete via `prepare_acceptance` / `record_acceptance` without mutating `task_events` on pre-check failure.
   - However, `prepare_acceptance` returns `None` whenever `not contract or contract == "local-only"`.
   - When a caller invoked `complete_task(conn, task_id, require_blocked_unclaimed=True)` (or CLI `hermes kanban complete <id> --if-blocked-unclaimed`) with empty/whitespace `result`, empty `summary`, empty stored `tasks.result`, and no PR `completion_contract` (`acceptance is None`), `complete_task` skipped `_gate_empty_completion`, updated `tasks.status = 'done'` with `result = NULL`, skipped `_synthesize_ended_run` when `metadata` was empty, and emitted a `"completed"` event with no evidence.
2. **Finding 2 — `_probe_fork_bomb` / `_git_is_trampoline` uncaught exceptions (`hermes_cli/update_cmd_git.py`)**:
   - `_git_is_trampoline` documents `"Never raises; unknown states report False so a probe failure can't block an update"` and delegates directly to `_probe_fork_bomb(git_cmd) is True`.
   - Narrowing `_probe_fork_bomb`'s handler from `except Exception:` to `except (OSError, subprocess.SubprocessError):` while resolving `_m().PROJECT_ROOT` and `Path(root).is_dir()` inside the `try:` block left non-OS lookup and type errors unhandled.
   - Any `ImportError`, `AttributeError`, `TypeError`, `ValueError`, or `RuntimeError` raised by `_m()`, `PROJECT_ROOT` resolution, `Path(root)`, `run_git`, or output decoding escaped `_probe_fork_bomb` and crashed `_git_is_trampoline`.
3. **Finding 3 — Unverifiable `PR_BODY.md` statements**:
   - The previous PR description cited historical commit SHAs, external lane notes, out-of-diff test pass counts, and generalized workflow prose that could not be verified solely from `DIFF.patch` and the changed files in the review packet.

## Structural fix

1. **Bind empty-completion exemption strictly to verified PR acceptance (`hermes_cli/kanban_completion_guards.py`, `hermes_cli/kanban_db.py`)**:
   - Add `audit_failure: bool = True` to `_gate_empty_completion` in `hermes_cli/kanban_completion_guards.py` (matching `_gate_created_cards`), suppressing the `completion_blocked_empty_result` write transaction when `audit_failure=False` so rejected `require_blocked_unclaimed` calls remain mutation-free while raising `EmptyCompletionError`.
   - In `complete_task` (`hermes_cli/kanban_db.py`), keep `_gate_empty_completion` before `prepare_acceptance` when `not require_blocked_unclaimed`, and enforce `_gate_empty_completion(conn, task_id, result=result, summary=summary, audit_failure=False)` whenever `require_blocked_unclaimed and acceptance is None`.
   - Extract `_synthesized_run_handoff` into `hermes_cli/kanban_completion_guards.py` so never-claimed PR-accepted completions also preserve a synthesized run record (`published_pr` metadata) even when `completion_contract` was pre-bound to a PR URL and `metadata` was omitted, while keeping `complete_task` compact.
2. **Restore total non-raising boundary in `_probe_fork_bomb` (`hermes_cli/update_cmd_git.py`)**:
   - Enclose the full probe evaluation (including `result.stdout`/`result.stderr` inspection) in `try ... except Exception as exc:` and log with `logger.debug("run_git fork bomb probe failed: %s", exc, exc_info=True)` before returning `None`, satisfying both `_git_is_trampoline`'s `"Never raises"` contract for every `Exception` subclass and the `BLE001`/`S110` code-health traceback-logging requirement.
3. **Ground `PR_BODY.md` strictly in diff-verifiable facts and protected-path impact**:
   - Replace historical runtime claims in PR #4's body with the exact `git diff --name-status` file inventory, module-by-module summary, touched test file inventory, and explicit production/CI impact statement for the `.github/workflows/*` changes.

## Proof

1. **Kanban empty-completion contract proof (`tests/hermes_cli/test_kanban_pr_acceptance.py`, `tests/hermes_cli/test_kanban_complete_blocked_unclaimed_cli.py`)**:
   - Library and CLI regression tests verify that `complete_task(..., require_blocked_unclaimed=True)` with empty/whitespace `result` and `summary` and no PR `completion_contract` (`None` or `"local-only"`, even if `metadata` is passed) raises `EmptyCompletionError` (and CLI exits 1) without mutating `tasks`, `task_runs`, or `task_events`, whereas a blocked unclaimed task with a valid PR `completion_contract` and green checks succeeds and records both `pr_acceptance` and a synthesized run.
2. **Non-raising trampoline probe proof (`tests/hermes_cli/test_cmd_update.py`)**:
   - Parametrized unit tests verify that `_probe_fork_bomb` returns `None` and `_git_is_trampoline` returns `False` without raising across `ImportError`, `AttributeError`, `TypeError`, `ValueError`, and `RuntimeError` raised during `_m()` / `PROJECT_ROOT` lookup or `run_git` execution.
3. **Static & lint gates**:
   - `ruff check .`, `scripts/check --only health`, `scripts/check-windows-footguns.py --all`, `scripts/check_bash_shebangs.py`, `scripts/check_no_tmp_literals.py`, and secret scan exit 0.
