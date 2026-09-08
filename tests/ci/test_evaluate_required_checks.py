"""Behavior tests for the required-check aggregate evaluator."""

from pathlib import Path

import pytest

from scripts.ci.evaluate_required_checks import evaluate_required_checks, main


EXPECTED = ("detect", "tests", "lint")


def test_success_and_intentional_path_skips_pass() -> None:
    result = evaluate_required_checks(
        {
            "detect": {"result": "success"},
            "tests": {"result": "success"},
            "lint": {"result": "skipped"},
        },
        EXPECTED,
    )

    assert result.invalid == ()
    assert result.compact == {
        "detect": "success",
        "tests": "success",
        "lint": "skipped",
    }


@pytest.mark.parametrize("result", ["failure", "cancelled", "unknown", None])
def test_non_terminal_success_results_fail_closed(result: str | None) -> None:
    evaluation = evaluate_required_checks(
        {
            "detect": {"result": "success"},
            "tests": {"result": result},
            "lint": {"result": "skipped"},
        },
        EXPECTED,
    )

    assert evaluation.invalid == ("tests",)


def test_missing_and_unexpected_jobs_fail_closed() -> None:
    evaluation = evaluate_required_checks(
        {
            "detect": {"result": "success"},
            "tests": {"result": "success"},
            "surprise": {"result": "success"},
        },
        EXPECTED,
    )

    assert evaluation.missing == ("lint",)
    assert evaluation.unexpected == ("surprise",)


def test_cli_writes_diagnostic_output_before_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "github-output"
    monkeypatch.setenv(
        "NEEDS",
        '{"detect":{"result":"success"},"tests":{"result":"cancelled"},'
        '"lint":{"result":"skipped"}}',
    )
    monkeypatch.setenv("EXPECTED_NEEDS", '["detect","tests","lint"]')
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert main() == 1
    assert output.read_text(encoding="utf-8").startswith("needs-json=")
