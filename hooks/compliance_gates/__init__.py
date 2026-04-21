"""Compliance gates package — deterministic code checks, no LLM calls.

Exports:
    GateResult      — shared result dataclass
    gate_letter_content  — CROA/FCRA letter checks
    gate_sms_send        — TCPA consent + time-window checks (lazy: imports pytz on first use)
    gate_status_change   — CRC state-machine + event-required checks

Usage from a Hermes skill::

    from hooks.compliance_gates import (
        gate_letter_content,
        gate_sms_send,
        gate_status_change,
        GateResult,
    )

    result = gate_letter_content(letter_body, client_data, dispute_items)
    if not result:
        raise ComplianceError(result.failures)

Note: ``gate_sms_send`` is lazily imported to avoid loading ``pytz`` for
consumers that only need letter or status gates.
"""

from .base import GateResult
from .letter import gate_letter_content
from .status import gate_status_change

__all__ = [
    "GateResult",
    "gate_letter_content",
    "gate_sms_send",
    "gate_status_change",
]

_lazy_attrs = {"gate_sms_send": ("hooks.compliance_gates.sms", "gate_sms_send")}


def __getattr__(name: str):
    if name in _lazy_attrs:
        module_path, attr = _lazy_attrs[name]
        import importlib
        mod = importlib.import_module(module_path)
        obj = getattr(mod, attr)
        # Cache on this module so subsequent accesses skip __getattr__
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
