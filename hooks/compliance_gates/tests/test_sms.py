"""Unit tests for gate_sms_send.

Fixtures:
  - PASS: opted-in client, local time mocked to 10:00 AM
  - FAIL: not opted-in
  - FAIL: outside time window (11 PM)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pytest
from unittest.mock import patch
from datetime import datetime, timezone
import pytz

from hooks.compliance_gates.sms import gate_sms_send


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CLEAN_MESSAGE = (
    "Hi John, this is Carl from Credit Edit Solutions. "
    "Just a reminder your next dispute round is ready. "
    "Reply STOP to opt out."
)

CONSENT_OK = {
    "sms_opted_in": True,
    "opt_in_date": "2026-01-15",
}


def _make_mock_now(hour: int, tz_name: str = "America/New_York") -> datetime:
    """Return a timezone-aware datetime at the given hour in the given tz."""
    tz = pytz.timezone(tz_name)
    naive = datetime(2026, 4, 20, hour, 0, 0)
    return tz.localize(naive)


# ---------------------------------------------------------------------------
# PASS: opted-in, 10 AM local time
# ---------------------------------------------------------------------------

def test_sms_pass_opted_in_daytime():
    """Opted-in client at 10 AM local time with clean message should pass."""
    tz_name = "America/New_York"
    mock_now = _make_mock_now(10, tz_name)

    with patch("hooks.compliance_gates.sms.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now
        result = gate_sms_send(CLEAN_MESSAGE, "+15555551234", CONSENT_OK, tz_name)

    assert result.passed is True, f"Expected pass, got failures: {result.failures}"
    assert result.failures == []
    assert "TCPA_consent" in result.rule_versions


# ---------------------------------------------------------------------------
# FAIL: not opted-in
# ---------------------------------------------------------------------------

CONSENT_NOT_OPTED_IN = {
    "sms_opted_in": False,
    "opt_in_date": "2026-01-15",
}


def test_sms_fail_not_opted_in():
    """Client with sms_opted_in=False must fail TCPA consent check."""
    tz_name = "America/New_York"
    mock_now = _make_mock_now(10, tz_name)

    with patch("hooks.compliance_gates.sms.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now
        result = gate_sms_send(CLEAN_MESSAGE, "+15555551234", CONSENT_NOT_OPTED_IN, tz_name)

    assert result.passed is False
    assert any("sms_opted_in" in f or "TCPA_consent" in f for f in result.failures), (
        f"Expected consent failure, got: {result.failures}"
    )


# ---------------------------------------------------------------------------
# FAIL: outside time window (11 PM)
# ---------------------------------------------------------------------------

def test_sms_fail_outside_time_window():
    """Sending at 11 PM local time must fail the time-window check."""
    tz_name = "America/New_York"
    mock_now = _make_mock_now(23, tz_name)  # 11 PM

    with patch("hooks.compliance_gates.sms.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now
        result = gate_sms_send(CLEAN_MESSAGE, "+15555551234", CONSENT_OK, tz_name)

    assert result.passed is False
    assert any("time_window" in f.lower() or "23:00" in f or "window" in f.lower() for f in result.failures), (
        f"Expected time-window failure, got: {result.failures}"
    )
