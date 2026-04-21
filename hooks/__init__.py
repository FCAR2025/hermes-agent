"""Hermes agent hooks package.

Compliance gates are mandatory pre-dispatch checks for any skill that
sends letters, SMS, or changes CRC client status.

Quick import::

    from hooks.compliance_gates import (
        gate_letter_content,
        gate_sms_send,
        gate_status_change,
        GateResult,
    )

Note: ``gate_sms_send`` (and anything that re-exports it from this package)
is lazily imported to avoid loading ``pytz`` for callers that only need
letter or status gates.
"""

from .compliance_gates import (
    GateResult,
    gate_letter_content,
    gate_status_change,
)

__all__ = [
    "GateResult",
    "gate_letter_content",
    "gate_sms_send",
    "gate_status_change",
]

_lazy_attrs = {"gate_sms_send": ("hooks.compliance_gates", "gate_sms_send")}


def __getattr__(name: str):
    if name in _lazy_attrs:
        module_path, attr = _lazy_attrs[name]
        import importlib
        mod = importlib.import_module(module_path)
        obj = getattr(mod, attr)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
