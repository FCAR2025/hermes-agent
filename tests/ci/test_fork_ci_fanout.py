"""Tests for fork CI shard fanout and run-workspace-checks.mjs slicing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

import pytest
from ruamel.yaml import YAML

from tests.ci._gha_expr import condition, evaluate, render

ROOT = Path(__file__).resolve().parents[2]
TESTS_WORKFLOW = ROOT / ".github/workflows/tests.yml"
JS_TESTS_WORKFLOW = ROOT / ".github/workflows/js-tests.yml"
RUN_CHECKS_SCRIPT = ROOT / ".github/scripts/run-workspace-checks.mjs"


def _load_yaml(path: Path) -> dict:
    return YAML(typ="base").load(path.read_text(encoding="utf-8"))


def test_tests_workflow_matrix_and_env_fanout():
    workflow = _load_yaml(TESTS_WORKFLOW)
    job = workflow["jobs"]["test"]

    ctx_upstream = {
        "github": {"repository": "NousResearch/hermes-agent"},
        "matrix": {"slice": 1},
    }
    ctx_fork = {
        "github": {"repository": "FCAR2025/hermes-agent"},
        "matrix": {"slice": 1},
    }

    # Strategy matrix slices
    upstream_slices = evaluate(str(job["strategy"]["matrix"]["slice"]), ctx_upstream)
    assert upstream_slices == [1, 2, 3]

    fork_slices = evaluate(str(job["strategy"]["matrix"]["slice"]), ctx_fork)
    assert fork_slices == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

    # Rendered job name
    assert render(job["name"], ctx_upstream) == "Run tests (1/3)"
    assert render(job["name"], ctx_fork) == "Run tests (1/12)"

    # Step "Run tests" env
    run_tests_step = next(s for s in job["steps"] if s.get("name") == "Run tests")
    assert render(run_tests_step["env"]["HERMES_TEST_SLICE"], ctx_upstream) == "1/3"
    assert render(run_tests_step["env"]["HERMES_TEST_SLICE"], ctx_fork) == "1/12"

    assert int(evaluate(str(run_tests_step["env"]["HERMES_TEST_WORKERS"]), ctx_upstream)) == 32
    assert int(evaluate(str(run_tests_step["env"]["HERMES_TEST_WORKERS"]), ctx_fork)) == 3


def test_js_tests_workflow_matrix_concurrency_and_slice():
    workflow = _load_yaml(JS_TESTS_WORKFLOW)
    job = workflow["jobs"]["check"]

    ctx_upstream = {
        "github": {"repository": "NousResearch/hermes-agent"},
        "matrix": {"slice": 1},
    }
    ctx_fork = {
        "github": {"repository": "FCAR2025/hermes-agent"},
        "matrix": {"slice": 1},
    }

    # Strategy matrix slices
    assert "strategy" in job
    upstream_slices = evaluate(str(job["strategy"]["matrix"]["slice"]), ctx_upstream)
    assert upstream_slices == [1]

    fork_slices = evaluate(str(job["strategy"]["matrix"]["slice"]), ctx_fork)
    assert fork_slices == [1, 2, 3, 4]

    # Rendered job name
    assert render(job["name"], ctx_upstream) == "JS & TS checks (1/1)"
    assert render(job["name"], ctx_fork) == "JS & TS checks (1/4)"

    # Step "Run all workspace checks"
    run_step = next(s for s in job["steps"] if s.get("name") == "Run all workspace checks")
    upstream_run = render(run_step["run"], ctx_upstream)
    assert "--concurrency 11" in upstream_run
    assert "--slice 1/1" in upstream_run

    fork_run = render(run_step["run"], ctx_fork)
    assert "--concurrency 3" in fork_run
    assert "--slice 1/4" in fork_run


def test_js_tests_slice_gating_and_cache_parity():
    workflow = _load_yaml(JS_TESTS_WORKFLOW)
    job = workflow["jobs"]["check"]
    steps = job["steps"]

    restore_step = next(s for s in steps if s.get("id") == "node-modules-cache")
    save_step = next(s for s in steps if s.get("name") == "Save node_modules cache")

    assert restore_step["uses"].startswith("actions/cache/restore@")
    assert save_step["uses"].startswith("actions/cache/save@")

    # Identical cached paths and key
    restore_paths = [p.strip() for p in restore_step["with"]["path"].splitlines() if p.strip()]
    save_paths = [p.strip() for p in save_step["with"]["path"].splitlines() if p.strip()]
    assert restore_paths == save_paths
    assert restore_step["with"]["key"].strip() == save_step["with"]["key"].strip()

    # Save step runs only when cache missed AND slice == 1
    hit_ctx_s1 = {
        "steps": {"node-modules-cache": {"outputs": {"cache-hit": "true"}}},
        "matrix": {"slice": 1},
    }
    miss_ctx_s1 = {
        "steps": {"node-modules-cache": {"outputs": {"cache-hit": "false"}}},
        "matrix": {"slice": 1},
    }
    miss_ctx_s2 = {
        "steps": {"node-modules-cache": {"outputs": {"cache-hit": "false"}}},
        "matrix": {"slice": 2},
    }
    assert not condition(save_step["if"], hit_ctx_s1)
    assert condition(save_step["if"], miss_ctx_s1)
    assert not condition(save_step["if"], miss_ctx_s2)

    # WhatsApp bridge steps gated to matrix.slice == 1
    wa_install = next(s for s in steps if s.get("name") == "Install WhatsApp bridge dependencies")
    wa_test = next(s for s in steps if s.get("name") == "Run WhatsApp bridge node tests")

    assert condition(wa_install["if"], {"matrix": {"slice": 1}})
    assert not condition(wa_install["if"], {"matrix": {"slice": 2}})
    assert condition(wa_test["if"], {"matrix": {"slice": 1}})
    assert not condition(wa_test["if"], {"matrix": {"slice": 3}})


@pytest.fixture
def fake_npm_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub_npm = bin_dir / "npm"

    # Multi-workspace sample returned in unsorted order
    mock_workspaces = [
        {"location": "web", "scripts": {"check": "vitest"}},
        {
            "location": "apps/desktop",
            "scripts": {
                "check:test:ui": "vitest",
                "check:test:desktop:platforms": "vitest",
                "check:test:desktop:all": "vitest",
                "check:lint": "eslint .",
                "check": "npm run check:test:ui && npm run check:lint",
            },
        },
        {"location": "ui-tui", "scripts": {"check": "vitest"}},
        {"location": "packages/pkg2", "scripts": {"check": "vitest"}},
        {"location": "apps/other1", "scripts": {"check": "vitest"}},
        {"location": "packages/pkg1", "scripts": {"check": "vitest"}},
        {"location": "apps/other2", "scripts": {"check": "vitest"}},
    ]

    stub_script = f"""#!/usr/bin/env python3
import json
import os
import sys

if sys.argv[1:] == ["query", ".workspace"]:
    if os.environ.get("MOCK_NPM_EMPTY") == "1":
        print("[]")
    else:
        print(json.dumps({json.dumps(mock_workspaces)}))
    sys.exit(0)

# Stand-in for npm run
sys.exit(0)
"""
    stub_npm.write_text(stub_script, encoding="utf-8")
    stub_npm.chmod(stub_npm.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    return env


def test_run_workspace_checks_script_slicing(fake_npm_env):
    def run_script(*args, env_extra=None):
        env = {**fake_npm_env, **(env_extra or {})}
        return subprocess.run(
            ["node", str(RUN_CHECKS_SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )

    # 1. Full list without slice
    res_full = run_script("--list")
    assert res_full.returncode == 0, res_full.stderr
    full_units = [line.strip() for line in res_full.stdout.splitlines() if line.strip()]
    assert len(full_units) == 10

    # 2. Slices 1/4 through 4/4
    sliced_lists = []
    sliced_sets = []
    for k in range(1, 5):
        res = run_script("--list", f"--slice={k}/4")
        assert res.returncode == 0, res.stderr
        units = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        assert len(units) > 0, f"Slice {k}/4 should be non-empty"
        # Each slice should contain at most 1 apps/desktop unit
        desktop_units = [u for u in units if "apps/desktop" in u]
        assert len(desktop_units) <= 1, f"Slice {k}/4 has >1 desktop unit: {desktop_units}"
        sliced_lists.append(units)
        sliced_sets.append(set(units))

    # Pairwise disjoint
    for i in range(len(sliced_sets)):
        for j in range(i + 1, len(sliced_sets)):
            assert sliced_sets[i].isdisjoint(sliced_sets[j]), f"Collision between {i+1} and {j+1}"

    # Union equals full
    all_sliced = set().union(*sliced_sets)
    assert all_sliced == set(full_units)

    # Verify space syntax --slice 1/4
    res_space = run_script("--list", "--slice", "1/4")
    assert res_space.returncode == 0
    assert [line.strip() for line in res_space.stdout.splitlines() if line.strip()] == sliced_lists[0]

    # 3. Slice 1/1 equals full list
    res_1_1 = run_script("--list", "--slice", "1/1")
    assert res_1_1.returncode == 0
    assert [line.strip() for line in res_1_1.stdout.splitlines() if line.strip()] == full_units

    # 4. N > units.length (e.g. 12/12 with 10 units) exits 0 cleanly
    res_empty_slice_list = run_script("--list", "--slice", "12/12")
    assert res_empty_slice_list.returncode == 0
    assert res_empty_slice_list.stdout.strip() == ""

    res_empty_slice_run = run_script("--slice", "12/12")
    assert res_empty_slice_run.returncode == 0
    assert "0 checks assigned" in res_empty_slice_run.stdout
    assert "nothing to run" in res_empty_slice_run.stdout

    # 5. Empty workspaces still fails closed
    res_no_units = run_script("--slice", "1/4", env_extra={"MOCK_NPM_EMPTY": "1"})
    assert res_no_units.returncode != 0
    assert "No workspace package declares a check script" in res_no_units.stderr

    # 6. Invalid --slice inputs exit non-zero
    for bad_slice in ("0/4", "5/4", "1/0", "-1/4", "foo", "1/4/2"):
        res_bad = run_script("--slice", bad_slice)
        assert res_bad.returncode != 0, f"Expected non-zero for --slice {bad_slice}"

    res_missing_arg = run_script("--slice")
    assert res_missing_arg.returncode != 0
