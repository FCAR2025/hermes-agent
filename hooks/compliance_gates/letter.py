"""Compliance gate: letter content checks.

gate_letter_content(letter_body, client_data, dispute_items) -> GateResult

Hard checks (fail-closed):
  1. CROA §1679b disclosure — cancellation-rights language must be present.
  2. CROA no-advance-fee / no-result-guarantee — forbidden phrases regex.
  3. FCRA §611 verifiable-claim — each disputed item must have specific,
     non-vague grounds with minimum substantive length.
  4. Furnisher-on-report — each disputed furnisher name must appear in
     client_data['report_furnishers'].

All checks operate solely on the inputs provided. No network or LLM calls.
"""

from __future__ import annotations

import re
import string
from typing import Any

from .base import GateResult

# ---------------------------------------------------------------------------
# Furnisher matching helpers
# ---------------------------------------------------------------------------

# Known bureau abbreviations → canonical lowercase name fragment.
# Alias lookup is applied before any matching so "TU" → "transunion", etc.
_FURNISHER_ALIASES: dict[str, str] = {
    "TU": "transunion",
    "EXP": "experian",
    "EQ": "equifax",
    "EFX": "equifax",
}

_PUNCT_TABLE = str.maketrans(string.punctuation, " " * len(string.punctuation))


def _canonicalize_furnisher(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, apply alias table.

    Args:
        name: Raw furnisher name (e.g. "TransUnion, LLC" or "TU").

    Returns:
        Normalized string (e.g. "transunion llc" or "transunion").
    """
    stripped = name.strip()
    # Alias match is uppercase-exact on the raw stripped value
    upper = stripped.upper()
    if upper in _FURNISHER_ALIASES:
        return _FURNISHER_ALIASES[upper]
    normalized = stripped.lower().translate(_PUNCT_TABLE)
    return " ".join(normalized.split())


def _matches_furnisher(disputed: str, report_furnisher: str) -> bool:
    """Return True when *disputed* is a legitimate match for *report_furnisher*.

    Matching rules (in order):
      1. Exact match after canonicalization → PASS.
      2. Token-set match: all tokens in the shorter canonical form appear as
         whole words in the longer canonical form, with ≥ 80 % token overlap
         relative to the shorter side → PASS.
      3. Everything else → FAIL.

    Args:
        disputed: Furnisher name from the dispute item.
        report_furnisher: Furnisher name from the client credit report.

    Returns:
        True if the names refer to the same furnisher, False otherwise.
    """
    canon_d = _canonicalize_furnisher(disputed)
    canon_r = _canonicalize_furnisher(report_furnisher)

    # Rule 1 — exact
    if canon_d == canon_r:
        return True

    # Rule 2 — token-set overlap (whole-word, short-side majority)
    tokens_d = set(canon_d.split())
    tokens_r = set(canon_r.split())
    shorter = tokens_d if len(tokens_d) <= len(tokens_r) else tokens_r
    longer_str = canon_r if len(tokens_d) <= len(tokens_r) else canon_d

    if not shorter:
        return False

    # Each token in the shorter set must appear as a whole word in the longer string
    overlap = sum(
        1 for tok in shorter
        if re.search(r"\b" + re.escape(tok) + r"\b", longer_str)
    )
    ratio = overlap / len(shorter)
    return ratio >= 0.80

# ---------------------------------------------------------------------------
# Rule version registry
# ---------------------------------------------------------------------------
_RULE_VERSIONS: dict[str, str] = {
    "CROA§1679b": "2026-04",
    "CROA§1679b_no_advance_fee": "2026-04",
    "FCRA§611": "2026-04",
    "furnisher_on_report": "2026-04",
}

# ---------------------------------------------------------------------------
# CROA §1679b — cancellation-rights disclosure patterns
# Must contain at minimum one of these patterns (case-insensitive).
# ---------------------------------------------------------------------------
_CROA_DISCLOSURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"right\s+to\s+cancel",
        re.IGNORECASE,
    ),
    re.compile(
        r"cancel\s+(this\s+)?(agreement|contract|service)\s+(at\s+any\s+time|within\s+\d+\s+days?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"cancellation\s+rights?",
        re.IGNORECASE,
    ),
    re.compile(
        r"you\s+may\s+cancel\b",
        re.IGNORECASE,
    ),
]

# ---------------------------------------------------------------------------
# CROA forbidden phrases — result guarantees / advance-fee promises
# ---------------------------------------------------------------------------
_FORBIDDEN_PHRASES: list[re.Pattern[str]] = [
    re.compile(r"guaranteed?\s+removal", re.IGNORECASE),
    re.compile(r"promise\s+to\s+delete", re.IGNORECASE),
    re.compile(r"100\s*%\s+removal\s+guaranteed", re.IGNORECASE),
    re.compile(r"guaranteed?\s+to\s+(improve|raise|boost)\s+(your\s+)?credit", re.IGNORECASE),
    re.compile(r"we\s+guarantee\s+results?", re.IGNORECASE),
    re.compile(r"money.?back\s+guarantee\s+on\s+(deletion|removal|credit)", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# FCRA §611 vague-grounds patterns — insufficient claim language
# ---------------------------------------------------------------------------
_VAGUE_GROUNDS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*something\s+is\s+wrong\s*$", re.IGNORECASE),
    re.compile(r"^\s*i\s+don'?t\s+(recognize|know)\s+this\s*$", re.IGNORECASE),
    re.compile(r"^\s*this\s+is\s+(incorrect|wrong|inaccurate)\s*$", re.IGNORECASE),
    re.compile(r"^\s*please\s+(fix|correct|remove)\s+this\s*$", re.IGNORECASE),
    re.compile(r"^\s*dispute\s*$", re.IGNORECASE),
]

# Minimum character length for a grounds statement to be considered substantive
_MIN_GROUNDS_LENGTH = 20


def _check_croa_disclosure(letter_body: str) -> str | None:
    """Return failure message if CROA cancellation-rights disclosure is absent."""
    for pattern in _CROA_DISCLOSURE_PATTERNS:
        if pattern.search(letter_body):
            return None  # found — passes
    return (
        "CROA§1679b: Letter missing required cancellation-rights disclosure. "
        "Must contain language such as 'right to cancel', 'you may cancel', "
        "or 'cancellation rights'."
    )


def _check_forbidden_phrases(letter_body: str) -> list[str]:
    """Return list of failure messages for each forbidden phrase found."""
    failures = []
    for pattern in _FORBIDDEN_PHRASES:
        m = pattern.search(letter_body)
        if m:
            failures.append(
                f"CROA§1679b_no_advance_fee: Forbidden phrase detected: "
                f"'{m.group(0)}'. Remove result-guarantee or advance-fee language."
            )
    return failures


def _check_verifiable_claims(dispute_items: list[dict[str, Any]]) -> list[str]:
    """Return failure messages for dispute items with vague or missing grounds."""
    failures = []
    for idx, item in enumerate(dispute_items):
        grounds: str = item.get("grounds", item.get("reason", item.get("basis", "")))
        grounds = str(grounds).strip()

        item_label = item.get("furnisher", item.get("creditor", f"item[{idx}]"))

        if not grounds:
            failures.append(
                f"FCRA§611: Dispute item '{item_label}' has no grounds stated. "
                "Each item must have specific, verifiable grounds."
            )
            continue

        # Check if grounds is too vague via pattern match
        is_vague = any(p.match(grounds) for p in _VAGUE_GROUNDS_PATTERNS)
        if is_vague:
            failures.append(
                f"FCRA§611: Dispute item '{item_label}' grounds are too vague: "
                f"'{grounds}'. State specific verifiable facts."
            )
            continue

        # Check minimum length
        if len(grounds) < _MIN_GROUNDS_LENGTH:
            failures.append(
                f"FCRA§611: Dispute item '{item_label}' grounds too brief "
                f"({len(grounds)} chars, min {_MIN_GROUNDS_LENGTH}): '{grounds}'. "
                "Provide specific, verifiable claim."
            )

    return failures


def _check_furnishers_on_report(
    dispute_items: list[dict[str, Any]],
    report_furnishers: list[str],
) -> list[str]:
    """Return failure messages for furnishers not found in the client's report."""
    failures = []

    for idx, item in enumerate(dispute_items):
        furnisher: str = str(
            item.get("furnisher", item.get("creditor", ""))
        ).strip()

        if not furnisher:
            failures.append(
                f"furnisher_on_report: Dispute item[{idx}] has no furnisher name. "
                "Each item must identify the furnisher."
            )
            continue

        found = any(_matches_furnisher(furnisher, rf) for rf in report_furnishers)
        if not found:
            failures.append(
                f"furnisher_on_report: '{furnisher}' not found in client's "
                "credit report furnisher list. Cannot dispute an account not on report."
            )

    return failures


def gate_letter_content(
    letter_body: str,
    client_data: dict[str, Any],
    dispute_items: list[dict[str, Any]],
) -> GateResult:
    """Check letter content for CROA and FCRA compliance.

    Args:
        letter_body: Full text of the dispute letter.
        client_data: Client record. Must contain 'report_furnishers' (list[str]).
        dispute_items: List of disputed items. Each must have 'furnisher'/'creditor'
            and 'grounds'/'reason'/'basis' keys.

    Returns:
        GateResult — passed=True only if all hard checks pass. Fail-closed.
    """
    failures: list[str] = []
    warnings: list[str] = []

    # 1. CROA disclosure
    disclosure_fail = _check_croa_disclosure(letter_body)
    if disclosure_fail:
        failures.append(disclosure_fail)

    # 2. Forbidden phrases
    failures.extend(_check_forbidden_phrases(letter_body))

    # 3. FCRA verifiable claims
    if not dispute_items:
        warnings.append("FCRA§611: No dispute items provided — verifiable-claim check skipped.")
    else:
        failures.extend(_check_verifiable_claims(dispute_items))

    # 4. Furnisher-on-report
    report_furnishers: list[str] = client_data.get("report_furnishers", [])
    if not isinstance(report_furnishers, list):
        failures.append(
            "furnisher_on_report: client_data['report_furnishers'] must be a list. "
            "Check that the client record is complete."
        )
    elif not report_furnishers:
        warnings.append(
            "furnisher_on_report: client_data['report_furnishers'] is empty — "
            "furnisher-on-report check skipped."
        )
    elif dispute_items:
        failures.extend(_check_furnishers_on_report(dispute_items, report_furnishers))

    passed = len(failures) == 0
    return GateResult(
        passed=passed,
        failures=failures,
        warnings=warnings,
        rule_versions=_RULE_VERSIONS,
    )
