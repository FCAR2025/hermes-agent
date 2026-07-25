"""Shared base types for compliance gates.

All gates return GateResult. Gates are deterministic code checks — no LLM calls.
Fail-closed: any hard failure sets passed=False.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateResult:
    """Result from a compliance gate check.

    Attributes:
        passed: True only when ALL hard checks pass.
        failures: List of hard-failure messages (each one is blocking).
        warnings: Non-blocking notices (logged but do not fail the gate).
        rule_versions: Maps rule identifier → version string, e.g.
            {"CROA§1679b": "2026-04", "FCRA§611": "2026-04"}.
        enforcement: Optional enforcement annotation (e.g. "playwright_only").
    """

    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rule_versions: dict[str, str] = field(default_factory=dict)
    enforcement: str | None = None

    def __bool__(self) -> bool:
        """Allow ``if not gate_result:`` idiom."""
        return self.passed

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"GateResult({status}, failures={self.failures!r}, "
            f"warnings={self.warnings!r}, enforcement={self.enforcement!r})"
        )
