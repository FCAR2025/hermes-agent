# Root Cause: Under-sharded fork CI fallback on 4-vCPU `ubuntu-latest`

Date: 2026-11-12.
Scope: GitHub Actions CI wall-clock performance on fork repositories (`tests.yml` and `js-tests.yml`).

## Class

**Under-sharded fork CI fallback on 4-vCPU `ubuntu-latest` runners**.

Upstream `NousResearch/hermes-agent` executes CI on dedicated high-performance Blacksmith runners (`blacksmith-32vcpu-ubuntu-2404`, 32 vCPUs). Because forks do not inherit upstream runner credentials or larger-runner subscriptions, fork workflow runs fall back to standard GitHub-hosted `ubuntu-latest` runners (4 vCPUs / 16 GB RAM).

While the fallback allows workflows to run on forks without failing runner allocation, retaining upstream's coarse sharding topology (`slice: [1, 2, 3]` for Python unit tests and 1 monolithic job for JavaScript/TypeScript checks) results in severe under-utilization of GitHub Actions' free concurrent job capacity.

## Mechanism

### 1. Python Unit Test Slices (`.github/workflows/tests.yml`)
- Upstream runs 3 slices on 32-vCPU runners with `HERMES_TEST_WORKERS: 32`, giving 96 parallel test workers. The entire ~50m10s test suite finishes in ~5–7 minutes wall-clock time.
- On forks falling back to `ubuntu-latest`, the workflow preserved `slice: [1, 2, 3]` with `HERMES_TEST_WORKERS: 2` (or 3). With only 3 slices and 2 workers per slice, exactly 6 workers processed 50m10s of test payload.
- In contrast, runner initialization and setup (`setup-pm`, SQLite checks, ripgrep install, dependency caches) takes only ~31s per runner.
- The 6 workers led to ~16–18 minutes wall-clock time per slice on 4-vCPU runners, with total wall-clock time often exceeding 40–50 minutes when accounting for load dilation, retries, and sequential job scheduling.

### 2. JavaScript / TypeScript Checks (`.github/workflows/js-tests.yml`)
- Upstream executes `js-tests.yml` as a single job on `blacksmith-32vcpu-ubuntu-2404` at `--concurrency 11`. With 32 cores, all 10 workspace checks run concurrently in ~6 minutes.
- On forks falling back to `ubuntu-latest`, `js-tests.yml` executed as a single un-sharded job at `--concurrency 2`. With `--concurrency 2`, heavy suites (such as `apps/desktop`'s UI, electron, platforms, and lint suites) ran sequentially.
- Running heavy Electron and Vitest checks sequentially on 4 vCPUs took over 30 minutes wall-clock time, compared to runner setup overhead of just ~42s (`actions/setup-pm`, Electron runtime preparation, node_modules restore).
- Furthermore, Vitest runtime history in `node_modules` was vulnerable to cache contamination across runs, and WhatsApp bridge dependencies were repeatedly installed and tested even though they are independent of the core desktop checks.

## Structural Fix

### 1. 12-way Python Slice Fanout on Forks (`tests.yml`)
- On forks (`github.repository != 'NousResearch/hermes-agent'`), expand the test matrix from 3 slices to 12 slices:
  `strategy.matrix.slice: ${{ fromJSON(github.repository == 'NousResearch/hermes-agent' && '[1, 2, 3]' || '[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]') }}`
- Update slice reporting and slice assignment:
  - `name: Run tests (${{ matrix.slice }}/${{ github.repository == 'NousResearch/hermes-agent' && 3 || 12 }})`
  - `HERMES_TEST_SLICE: ${{ matrix.slice }}/${{ github.repository == 'NousResearch/hermes-agent' && 3 || 12 }}`
  - `HERMES_TEST_WORKERS: ${{ github.repository == 'NousResearch/hermes-agent' && 32 || 3 }}`
- Slicing across 12 runners with 3 workers each provides 36 total workers on forks. Because setup takes only ~31s, the 50m10s payload finishes in ~4–5 minutes wall-clock time at $0 runner cost.
- Upstream `NousResearch/hermes-agent` behavior remains 100% unchanged (3 slices, `HERMES_TEST_SLICE: K/3`, `HERMES_TEST_WORKERS: 32`).

### 2. Deterministic Sorting & Slicing in `run-workspace-checks.mjs`
- In `discoverUnits()`, sort discovered workspace packages deterministically by code-unit path (`pkgs.sort((a, b) => (a.location < b.location ? -1 : a.location > b.location ? 1 : 0))`). Separate CI runners always agree 100% on unit ordering regardless of npm query output ordering.
- Add `--slice K/N` (and `--slice=K/N`) support with strict validation (`1 <= K <= N`, positive integers).
- Distribute units via round-robin modulo: `const units = sliceSpec ? allUnits.filter((_, idx) => idx % sliceSpec.count === sliceSpec.index - 1) : allUnits`.
- Because `apps/desktop` is sorted first, its 4 sub-scripts (`check:test:ui`, `check:test:desktop:platforms`, `check:test:desktop:all`, `check:lint`) map to indices 0, 1, 2, 3. In a 4-slice split, each slice receives exactly 1 heavy desktop check, perfectly balancing load across runners.
- Maintain empty-check validation: an empty workspace tree fails with exit 1 before slicing, while a valid slice with 0 assigned units (when `N > allUnits.length`) exits cleanly with exit 0.

### 3. 4-way JavaScript Check Fanout on Forks (`js-tests.yml`)
- Introduce matrix strategy to `jobs.check`:
  `slice: ${{ fromJSON(github.repository == 'NousResearch/hermes-agent' && '[1]' || '[1, 2, 3, 4]') }}`
- Set job name: `JS & TS checks (${{ matrix.slice }}/${{ github.repository == 'NousResearch/hermes-agent' && 1 || 4 }})`.
- Split cache into `actions/cache/restore` and `actions/cache/save` (gated by `if: steps.node-modules-cache.outputs.cache-hit != 'true' && matrix.slice == 1`) right after Electron runtime preparation, preventing cache write races across parallel runners and saving the cache before test execution generates Vitest history.
- Gate WhatsApp bridge installation and node tests with `if: matrix.slice == 1` so they execute exactly once per workflow run.
- Execute workspace checks with `--concurrency 3` and `--slice ${{ matrix.slice }}/4` on forks, and `--concurrency 11 --slice 1/1` on upstream.
- 4 slices at concurrency 3 complete all JS checks in ~5–7 minutes wall-clock time.

## Proof

1. Automated CI contract tests in `tests/ci/test_fork_ci_fanout.py` evaluating both repository contexts (`NousResearch/hermes-agent` and `FCAR2025/hermes-agent`):
   - Matrix slice evaluation (`[1, 2, 3]` vs `[1..12]` for tests, `[1]` vs `[1..4]` for js-tests).
   - Rendered job names, `HERMES_TEST_SLICE`, and `HERMES_TEST_WORKERS`.
   - Workspace checks CLI `--slice` parsing, pairwise disjoint distribution, desktop check balance, and error edge cases.
   - Cache key and path parity between restore and save actions.
2. Full `tests/ci/` suite passing without regression, strictly adhering to `test_js_tests_vitest_history.py` and `test_clean_worktree_check.py` requirements.
