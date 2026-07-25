"""Unit tests for gate_status_change.

Fixtures:
  - PASS: billing-completed event within last 24h for Pending→Active
  - FAIL: no recent event (event is 48h old)
  - FAIL: invalid transition (Lead→Graduated not in state machine)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pytest
from datetime import datetime, timedelta, timezone

from hooks.compliance_gates.status import gate_status_change


def _ts_hours_ago(hours: float) -> str:
    """Return an ISO 8601 UTC timestamp string N hours before now."""
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# PASS: billing-completed event within last 24h, Pending→Active
# ---------------------------------------------------------------------------

def test_status_pass_billing_completed_recent():
    """Pending→Active with a recent billing_completed event must pass."""
    evidence = [
        {
            "event_type": "billing_completed",
            "timestamp": _ts_hours_ago(2),  # 2 hours ago — well within window
            "notes": "Payment of $350 received via Stripe",
        }
    ]
    result = gate_status_change(
        client_id="client-001",
        from_state="Pending",
        to_state="Active",
        evidence_events=evidence,
    )
    assert result.passed is True, f"Expected pass, got failures: {result.failures}"
    assert result.failures == []
    assert result.enforcement == "playwright_only"
    assert "CRC_state_machine" in result.rule_versions
    assert "CRC_event_required" in result.rule_versions


# ---------------------------------------------------------------------------
# FAIL: no recent event (event is 48h old)
# ---------------------------------------------------------------------------

def test_status_fail_no_recent_event():
    """Pending→Active with only a 48h-old event must fail event-required check."""
    evidence = [
        {
            "event_type": "billing_completed",
            "timestamp": _ts_hours_ago(48),  # 48 hours ago — outside 24h window
        }
    ]
    result = gate_status_change(
        client_id="client-002",
        from_state="Pending",
        to_state="Active",
        evidence_events=evidence,
    )
    assert result.passed is False
    assert any("CRC_event_required" in f or "event" in f.lower() for f in result.failures), (
        f"Expected event-required failure, got: {result.failures}"
    )
    # enforcement annotation present regardless of pass/fail
    assert result.enforcement == "playwright_only"


# ---------------------------------------------------------------------------
# FAIL: invalid transition (Lead→Graduated)
# ---------------------------------------------------------------------------

def test_status_fail_invalid_transition():
    """Lead→Graduated is not in the state machine and must fail."""
    evidence = [
        {
            "event_type": "graduation_criteria_met",
            "timestamp": _ts_hours_ago(1),
        }
    ]
    result = gate_status_change(
        client_id="client-003",
        from_state="Lead",
        to_state="Graduated",
        evidence_events=evidence,
    )
    assert result.passed is False
    assert any("CRC_state_machine" in f or "not allowed" in f.lower() for f in result.failures), (
        f"Expected state-machine failure, got: {result.failures}"
    )
