"""Unit tests for gate_letter_content.

Fixtures:
  - PASS: compliant Round-2 dispute letter
  - FAIL: missing CROA disclosure
  - FAIL: forbidden phrase ("guaranteed removal")
  - FAIL: furnisher not on client report
"""

import sys
import os

# Allow running from repo root: python -m pytest hooks/compliance_gates/tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pytest
from hooks.compliance_gates.letter import gate_letter_content


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

COMPLIANT_LETTER = """
Dear Equifax Dispute Center,

I am writing to dispute the following items on my credit report as inaccurate
and unverifiable under the Fair Credit Reporting Act (FCRA), Section 611.

Account: Capital One Auto Finance
Account Number: ***1234
Reason: This account was paid in full and closed on 2024-06-15 per my records.
The balance of $4,200 reported as outstanding is factually incorrect.

Account: Midland Credit Management
Account Number: ***5678
Reason: I have no record of this debt originating from the stated creditor.
The date of first delinquency reported as 2022-01 conflicts with my records
showing account closure in 2021-08. Please provide the original signed agreement.

Please note: You have the right to cancel this agreement within 3 business days
of signing, without penalty or obligation. This is your right to cancel under
federal law.

Sincerely,
John Q. Client
"""

CLIENT_DATA_OK = {
    "report_furnishers": [
        "Capital One Auto Finance",
        "Midland Credit Management",
        "Experian",
    ]
}

DISPUTE_ITEMS_OK = [
    {
        "furnisher": "Capital One Auto Finance",
        "grounds": "Account was paid in full and closed on 2024-06-15; balance shown is incorrect.",
    },
    {
        "furnisher": "Midland Credit Management",
        "grounds": "No record of this debt; date of first delinquency conflicts with my own records.",
    },
]


# ---------------------------------------------------------------------------
# PASS fixture
# ---------------------------------------------------------------------------

def test_letter_pass_compliant():
    """A complete, compliant Round-2 dispute letter should pass all gates."""
    result = gate_letter_content(COMPLIANT_LETTER, CLIENT_DATA_OK, DISPUTE_ITEMS_OK)
    assert result.passed is True, f"Expected pass, got failures: {result.failures}"
    assert result.failures == []
    assert "CROA§1679b" in result.rule_versions
    assert "FCRA§611" in result.rule_versions


# ---------------------------------------------------------------------------
# FAIL: missing CROA disclosure
# ---------------------------------------------------------------------------

LETTER_NO_CROA = """
Dear Equifax Dispute Center,

I am writing to dispute inaccurate items on my credit report.

Account: Capital One Auto Finance
Reason: This account was paid in full on 2024-06-15; the balance shown is wrong.

Sincerely,
John Q. Client
"""


def test_letter_fail_missing_croa_disclosure():
    """Letter without cancellation-rights disclosure must fail."""
    result = gate_letter_content(LETTER_NO_CROA, CLIENT_DATA_OK, DISPUTE_ITEMS_OK[:1])
    assert result.passed is False
    assert any("CROA§1679b" in f and "cancellation" in f.lower() for f in result.failures), (
        f"Expected CROA disclosure failure, got: {result.failures}"
    )


# ---------------------------------------------------------------------------
# FAIL: forbidden phrase
# ---------------------------------------------------------------------------

LETTER_WITH_GUARANTEE = """
Dear Equifax Dispute Center,

We offer guaranteed removal of all negative items from your credit report.
We promise to delete any inaccurate entry within 30 days.

You have the right to cancel this agreement within 3 business days.

Account: Capital One Auto Finance
Reason: This account was paid in full on 2024-06-15; the balance shown is wrong.

Sincerely,
CES
"""


def test_letter_fail_forbidden_phrase():
    """Letter with 'guaranteed removal' must fail."""
    result = gate_letter_content(LETTER_WITH_GUARANTEE, CLIENT_DATA_OK, DISPUTE_ITEMS_OK[:1])
    assert result.passed is False
    assert any("guaranteed" in f.lower() or "forbidden" in f.lower() for f in result.failures), (
        f"Expected forbidden-phrase failure, got: {result.failures}"
    )


# ---------------------------------------------------------------------------
# FAIL: furnisher not on client report
# ---------------------------------------------------------------------------

DISPUTE_ITEMS_BAD_FURNISHER = [
    {
        "furnisher": "Unknown Debt Collectors LLC",
        "grounds": "This account does not belong to me and was never opened by me.",
    },
]


def test_letter_fail_furnisher_not_on_report():
    """Dispute item with furnisher absent from client report must fail."""
    result = gate_letter_content(COMPLIANT_LETTER, CLIENT_DATA_OK, DISPUTE_ITEMS_BAD_FURNISHER)
    assert result.passed is False
    assert any("furnisher_on_report" in f or "not found" in f for f in result.failures), (
        f"Expected furnisher-on-report failure, got: {result.failures}"
    )


# ---------------------------------------------------------------------------
# Regression: furnisher matching logic (P1.12)
# ---------------------------------------------------------------------------

def _make_items(furnisher: str) -> list[dict]:
    return [{"furnisher": furnisher, "grounds": "This account balance is factually incorrect per my records."}]


def _furnisher_check(disputed: str, report_furnisher: str) -> bool:
    """Run only the furnisher-on-report gate and return passed status."""
    from hooks.compliance_gates.letter import _matches_furnisher
    return _matches_furnisher(disputed, report_furnisher)


@pytest.mark.parametrize("disputed,report_furnisher,expected", [
    ("TS",           "TransUnion",                False),  # was PASS (the bug)
    ("TU",           "TransUnion",                True),   # alias
    ("TransUnion",   "TransUnion, LLC",            True),   # token subset
    ("Experian",     "Equifax",                   False),  # different bureau
    ("EXP",          "Experian",                  True),   # alias
    ("XYZ Corp",     "Transunion",                False),  # unrelated
    ("Transunion",   "TransUnion",                True),   # case-insensitive exact
])
def test_furnisher_match_regression(disputed: str, report_furnisher: str, expected: bool):
    """Regression table for furnisher matching — P1.12."""
    result = _furnisher_check(disputed, report_furnisher)
    assert result is expected, (
        f"_matches_furnisher({disputed!r}, {report_furnisher!r}) "
        f"expected {expected}, got {result}"
    )
