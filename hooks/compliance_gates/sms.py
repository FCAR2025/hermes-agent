"""Compliance gate: SMS send checks.

gate_sms_send(message, recipient_chat, client_consent, local_tz) -> GateResult

Hard checks (fail-closed):
  1. TCPA consent — sms_opted_in is True, opt_in_date present, not revoked.
  2. Time window — recipient's local time is 08:00–21:00 inclusive.
  3. No forbidden terms — manipulative urgency bait phrases blocked.

All checks operate solely on the inputs provided. No network or LLM calls.
Requires: pytz (standard on this deployment).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytz

from .base import GateResult

# ---------------------------------------------------------------------------
# Rule version registry
# ---------------------------------------------------------------------------
_RULE_VERSIONS: dict[str, str] = {
    "TCPA_consent": "2026-04",
    "TCPA_time_window": "2026-04",
    "TCPA_forbidden_terms": "2026-04",
}

_DEFAULT_TZ = "America/New_York"

# Window: 08:00 (inclusive) to 21:00 (inclusive per TCPA "before 9pm")
_WINDOW_START_HOUR = 8   # 08:00
_WINDOW_END_HOUR = 21    # 21:00 (calls/texts must stop before 9 PM = stop at 20:59)

# ---------------------------------------------------------------------------
# Forbidden urgency-bait terms (TCPA / consumer-protection heuristic)
# ---------------------------------------------------------------------------
_FORBIDDEN_TERM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"call\s+us\s+NOW\b", re.IGNORECASE),
    re.compile(r"ACT\s+NOW\b", re.IGNORECASE),
    re.compile(r"respond\s+immediately\b", re.IGNORECASE),
    re.compile(r"limited\s+time\s+offer\s*!", re.IGNORECASE),
    re.compile(r"you\s+(have\s+)?won\b", re.IGNORECASE),
    re.compile(r"URGENT\s*[:\-!]", re.IGNORECASE),
    re.compile(r"last\s+chance\b", re.IGNORECASE),
    re.compile(r"expire[sd]?\s+today\b", re.IGNORECASE),
    re.compile(r"don'?t\s+(miss|ignore|delay)\b", re.IGNORECASE),
    re.compile(r"click\s+now\b", re.IGNORECASE),
]


def _check_tcpa_consent(client_consent: dict[str, Any]) -> list[str]:
    """Return failure messages for TCPA consent deficiencies."""
    failures: list[str] = []

    opted_in = client_consent.get("sms_opted_in")
    if opted_in is not True:
        failures.append(
            "TCPA_consent: client_consent['sms_opted_in'] is not True. "
            f"Value: {opted_in!r}. TCPA requires explicit written consent."
        )

    opt_in_date = client_consent.get("opt_in_date")
    if not opt_in_date:
        failures.append(
            "TCPA_consent: client_consent['opt_in_date'] is missing. "
            "Consent must be timestamped to be auditable."
        )

    revoked = client_consent.get("opt_out_date") or client_consent.get("revoked")
    if revoked:
        failures.append(
            f"TCPA_consent: Consent has been revoked (opt_out_date/revoked={revoked!r}). "
            "Cannot send SMS after opt-out."
        )

    return failures


def _check_time_window(local_tz: str) -> list[str]:
    """Return failure messages if current time is outside 08:00–20:59 in local_tz."""
    failures: list[str] = []

    tz_name = local_tz if local_tz else _DEFAULT_TZ
    try:
        tz = pytz.timezone(tz_name)
    except pytz.exceptions.UnknownTimeZoneError:
        failures.append(
            f"TCPA_time_window: Unknown timezone '{tz_name}'. "
            "Cannot verify send-window compliance. Failing closed."
        )
        return failures

    now_local = datetime.now(tz=tz)
    hour = now_local.hour  # 0-23

    # Allowed: 8 <= hour < 21  (08:00:00 through 20:59:59)
    if hour < _WINDOW_START_HOUR or hour >= _WINDOW_END_HOUR:
        failures.append(
            f"TCPA_time_window: Current local time {now_local.strftime('%H:%M')} "
            f"({tz_name}) is outside the 08:00–20:59 permitted window. "
            "Do not send SMS outside allowed hours."
        )

    return failures


def _check_forbidden_terms(message: str) -> list[str]:
    """Return failure messages for each forbidden urgency-bait phrase found."""
    failures: list[str] = []
    for pattern in _FORBIDDEN_TERM_PATTERNS:
        m = pattern.search(message)
        if m:
            failures.append(
                f"TCPA_forbidden_terms: Forbidden urgency-bait phrase detected: "
                f"'{m.group(0)}'. Remove manipulative urgency language."
            )
    return failures


def gate_sms_send(
    message: str,
    recipient_chat: str,
    client_consent: dict[str, Any],
    local_tz: str = _DEFAULT_TZ,
) -> GateResult:
    """Check SMS send for TCPA compliance.

    Args:
        message: Text content of the SMS to be sent.
        recipient_chat: Recipient identifier (phone/chat ID) — used for audit trail.
        client_consent: Consent record. Expected keys:
            - sms_opted_in (bool): True if client has opted in.
            - opt_in_date (str|datetime): When consent was given.
            - opt_out_date / revoked (optional): Set if consent revoked.
        local_tz: IANA timezone string for the recipient's location.
            Defaults to 'America/New_York'.

    Returns:
        GateResult — passed=True only if all hard checks pass. Fail-closed.
    """
    failures: list[str] = []
    warnings: list[str] = []

    # 1. TCPA consent
    failures.extend(_check_tcpa_consent(client_consent))

    # 2. Time window
    failures.extend(_check_time_window(local_tz))

    # 3. Forbidden terms
    failures.extend(_check_forbidden_terms(message))

    if not recipient_chat:
        warnings.append("recipient_chat is empty — message destination unverified.")

    passed = len(failures) == 0
    return GateResult(
        passed=passed,
        failures=failures,
        warnings=warnings,
        rule_versions=_RULE_VERSIONS,
    )
