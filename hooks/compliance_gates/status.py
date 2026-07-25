"""Compliance gate: CRC status change checks.

gate_status_change(client_id, from_state, to_state, evidence_events) -> GateResult

Hard checks (fail-closed):
  1. Allowed transition — verified against the CRC state machine table.
  2. CRC-event-required — at least one evidence_event timestamped within 24h
     that is valid justification for this specific transition.

Enforcement annotation: result.enforcement == "playwright_only"
  The gate does not block the write itself, but callers MUST use Playwright
  to perform status updates (CRC API update_client_status silently fails).

All checks operate solely on the inputs provided. No network or LLM calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .base import GateResult

# ---------------------------------------------------------------------------
# Rule version registry
# ---------------------------------------------------------------------------
_RULE_VERSIONS: dict[str, str] = {
    "CRC_state_machine": "2026-04",
    "CRC_event_required": "2026-04",
}

# ---------------------------------------------------------------------------
# CRC state machine — from_state → frozenset of valid to_states
# Based on CRC Credit Repair Cloud lifecycle stages.
# ---------------------------------------------------------------------------
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # Lead has been created, awaiting initial contact
    "Lead": frozenset({"Prospect", "Inactive", "Cancelled"}),
    # Prospect has been contacted, evaluating fit
    "Prospect": frozenset({"Active", "Pending", "Inactive", "Cancelled"}),
    # Pending agreement / billing setup
    "Pending": frozenset({"Active", "Inactive", "Cancelled"}),
    # Active client — currently in dispute cycle
    "Active": frozenset({"Graduated", "Inactive", "Cancelled", "Dispute"}),
    # In active dispute round
    "Dispute": frozenset({"Active", "Graduated", "Inactive", "Cancelled"}),
    # Graduated — completed program
    "Graduated": frozenset({"Active", "Inactive"}),
    # Inactive — paused / no response
    "Inactive": frozenset({"Active", "Prospect", "Cancelled"}),
    # Terminal state
    "Cancelled": frozenset({"Active"}),  # re-activation allowed
}

# ---------------------------------------------------------------------------
# Events that justify specific transitions (from_state → to_state → required event types)
# A transition requires at least one evidence_event whose 'event_type' matches
# any entry in the set for that (from, to) pair.
# If a (from, to) pair is not listed, any recent event is accepted.
# ---------------------------------------------------------------------------
_TRANSITION_EVENT_REQUIREMENTS: dict[tuple[str, str], frozenset[str]] = {
    ("Pending", "Active"): frozenset({
        "billing_completed",
        "payment_received",
        "agreement_signed",
    }),
    ("Lead", "Prospect"): frozenset({
        "contact_made",
        "call_completed",
        "email_replied",
        "sms_replied",
    }),
    ("Prospect", "Active"): frozenset({
        "billing_completed",
        "payment_received",
        "agreement_signed",
    }),
    ("Active", "Graduated"): frozenset({
        "graduation_criteria_met",
        "dispute_cycle_completed",
        "target_score_reached",
    }),
    ("Active", "Dispute"): frozenset({
        "dispute_letter_sent",
        "dispute_initiated",
    }),
    ("Dispute", "Active"): frozenset({
        "dispute_response_received",
        "furnisher_responded",
        "credit_bureau_responded",
    }),
    ("Inactive", "Active"): frozenset({
        "billing_completed",
        "payment_received",
        "client_reactivated",
        "call_completed",
    }),
    ("Cancelled", "Active"): frozenset({
        "billing_completed",
        "payment_received",
        "client_reactivated",
    }),
}

_EVENT_WINDOW_HOURS = 24


def _parse_event_timestamp(ts: Any) -> datetime | None:
    """Parse a timestamp from an event dict. Returns UTC-aware datetime or None."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if isinstance(ts, (int, float)):
        # Unix timestamp
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        # Try ISO 8601 with Z suffix, then without tz
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S+00:00",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(ts, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None


def _check_allowed_transition(from_state: str, to_state: str) -> str | None:
    """Return failure message if the transition is not in the state machine."""
    allowed = _ALLOWED_TRANSITIONS.get(from_state)
    if allowed is None:
        return (
            f"CRC_state_machine: Unknown from_state '{from_state}'. "
            f"Known states: {sorted(_ALLOWED_TRANSITIONS.keys())}."
        )
    if to_state not in allowed:
        return (
            f"CRC_state_machine: Transition '{from_state}' → '{to_state}' is not allowed. "
            f"Valid targets from '{from_state}': {sorted(allowed)}."
        )
    return None


def _check_evidence_events(
    from_state: str,
    to_state: str,
    evidence_events: list[dict[str, Any]],
) -> str | None:
    """Return failure message if no qualifying recent event supports the transition."""
    now_utc = datetime.now(tz=timezone.utc)
    cutoff = now_utc - timedelta(hours=_EVENT_WINDOW_HOURS)

    # Determine which event types are required for this transition
    required_types = _TRANSITION_EVENT_REQUIREMENTS.get((from_state, to_state))

    for event in evidence_events:
        ts_raw = event.get("timestamp") or event.get("created_at") or event.get("ts")
        ts = _parse_event_timestamp(ts_raw)

        if ts is None:
            continue  # can't parse timestamp — skip this event

        if ts < cutoff:
            continue  # event is too old

        # Event is within the window. Check type if required.
        if required_types is not None:
            event_type = str(event.get("event_type", event.get("type", ""))).lower()
            if not any(req.lower() == event_type for req in required_types):
                continue  # type doesn't qualify this transition
        # Either no type requirement, or type matched
        return None  # found a qualifying event — passes

    # No qualifying event found
    if required_types is not None:
        return (
            f"CRC_event_required: No qualifying event within last {_EVENT_WINDOW_HOURS}h "
            f"for transition '{from_state}' → '{to_state}'. "
            f"Required event types: {sorted(required_types)}."
        )
    return (
        f"CRC_event_required: No evidence event within last {_EVENT_WINDOW_HOURS}h "
        f"to justify transition '{from_state}' → '{to_state}'. "
        "At least one recent activity event is required."
    )


def gate_status_change(
    client_id: str,
    from_state: str,
    to_state: str,
    evidence_events: list[dict[str, Any]],
) -> GateResult:
    """Check a CRC client status transition for compliance.

    Args:
        client_id: CRC client identifier (used for audit trail only).
        from_state: Current CRC status (e.g. "Pending", "Active").
        to_state: Desired CRC status (e.g. "Active", "Graduated").
        evidence_events: List of recent CRC events. Each event dict should have:
            - timestamp / created_at / ts: parseable datetime string or unix float.
            - event_type / type: string identifying the event kind.

    Returns:
        GateResult with enforcement="playwright_only" — callers MUST use Playwright
        to perform the actual status write (CRC API silently fails on status updates).
        Fail-closed.
    """
    failures: list[str] = []
    warnings: list[str] = []

    if not client_id:
        warnings.append("client_id is empty — audit trail will be incomplete.")

    # 1. Allowed transition
    transition_fail = _check_allowed_transition(from_state, to_state)
    if transition_fail:
        failures.append(transition_fail)
    else:
        # 2. Evidence event required (only check if transition is structurally valid)
        event_fail = _check_evidence_events(from_state, to_state, evidence_events)
        if event_fail:
            failures.append(event_fail)

    passed = len(failures) == 0
    return GateResult(
        passed=passed,
        failures=failures,
        warnings=warnings,
        rule_versions=_RULE_VERSIONS,
        enforcement="playwright_only",
    )
