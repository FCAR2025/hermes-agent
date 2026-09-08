"""Fail-closed evaluator for the CI workflow's required-check aggregate."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PASSING_RESULTS = frozenset({"success", "skipped"})


@dataclass(frozen=True)
class Evaluation:
    compact: dict[str, object]
    invalid: tuple[str, ...]
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not (self.invalid or self.missing or self.unexpected)


def evaluate_required_checks(
    needs: Mapping[str, Any], expected_names: Sequence[str]
) -> Evaluation:
    """Accept only the complete expected set of successful or skipped jobs."""
    expected = tuple(expected_names)
    if not expected or any(not isinstance(name, str) or not name for name in expected):
        raise ValueError("EXPECTED_NEEDS must contain non-empty job names")
    if len(set(expected)) != len(expected):
        raise ValueError("EXPECTED_NEEDS must not contain duplicates")

    actual_names = set(needs)
    expected_set = set(expected)
    compact: dict[str, object] = {}
    invalid: list[str] = []
    for name in sorted(actual_names):
        info = needs[name]
        result = info.get("result") if isinstance(info, Mapping) else None
        compact[name] = result
        if result not in _PASSING_RESULTS:
            invalid.append(name)

    return Evaluation(
        compact=compact,
        invalid=tuple(invalid),
        missing=tuple(sorted(expected_set - actual_names)),
        unexpected=tuple(sorted(actual_names - expected_set)),
    )


def _load_inputs() -> tuple[Mapping[str, Any], Sequence[str]]:
    needs = json.loads(os.environ["NEEDS"])
    expected = json.loads(os.environ["EXPECTED_NEEDS"])
    if not isinstance(needs, Mapping) or not isinstance(expected, list):
        raise ValueError("NEEDS must be an object and EXPECTED_NEEDS must be an array")
    return needs, expected


def main() -> int:
    try:
        needs, expected = _load_inputs()
        evaluation = evaluate_required_checks(needs, expected)
        output_path = Path(os.environ["GITHUB_OUTPUT"])
    except (KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"::error::Invalid required-check input: {exc}")
        return 1

    compact_json = json.dumps(evaluation.compact, separators=(",", ":"))
    print(f"needs-json={compact_json}")
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"needs-json={compact_json}\n")

    for name, result in evaluation.compact.items():
        icon = "✅" if result in _PASSING_RESULTS else "❌"
        print(f"{icon} {json.dumps(name)}: {json.dumps(result)}")

    if not evaluation.passed:
        details = {
            "invalid": evaluation.invalid,
            "missing": evaluation.missing,
            "unexpected": evaluation.unexpected,
        }
        print(f"::error::Required checks did not pass: {json.dumps(details)}")
        return 1

    print("All required checks passed or were intentionally skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
