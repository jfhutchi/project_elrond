"""Small label-free Prometheus registry for operational telemetry."""

from __future__ import annotations

import re
from decimal import Decimal

_METRIC_NAME = re.compile(r"^quantbot_[a-z][a-z0-9_]*$")
_FORBIDDEN_NAME_PARTS = ("credential", "password", "secret", "token", "api_key")


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _validate_name(name: str) -> None:
    if not _METRIC_NAME.fullmatch(name):
        raise ValueError("metric name must be normalized and quantbot-prefixed")
    if any(part in name for part in _FORBIDDEN_NAME_PARTS):
        raise ValueError("secret-bearing metric names are forbidden")


class OperationalMetrics:
    """Counters and gauges without labels, preventing secret-bearing dimensions."""

    def __init__(self) -> None:
        self._counters: dict[str, Decimal] = {}
        self._gauges: dict[str, Decimal] = {}

    def increment(self, name: str, amount: Decimal = Decimal("1")) -> None:
        _validate_name(name)
        if not amount.is_finite() or amount < 0:
            raise ValueError("counter increments must be nonnegative and finite")
        if name in self._gauges:
            raise ValueError(f"metric already registered as gauge: {name}")
        self._counters[name] = self._counters.get(name, Decimal("0")) + amount

    def set_gauge(self, name: str, value: Decimal) -> None:
        _validate_name(name)
        if not value.is_finite():
            raise ValueError("gauge values must be finite")
        if name in self._counters:
            raise ValueError(f"metric already registered as counter: {name}")
        self._gauges[name] = value

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for name in sorted(self._counters):
            lines.extend(
                (f"# TYPE {name} counter", f"{name} {_format_decimal(self._counters[name])}")
            )
        for name in sorted(self._gauges):
            lines.extend((f"# TYPE {name} gauge", f"{name} {_format_decimal(self._gauges[name])}"))
        return "\n".join(lines) + ("\n" if lines else "")
