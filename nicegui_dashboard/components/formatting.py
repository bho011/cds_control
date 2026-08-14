"""Reine Formatierungs-Helfer für die Dashboard-Anzeige, ohne UI-/Controller-Abhängigkeit."""

from __future__ import annotations

from typing import Any


def fmt(value: Any, unit: str = "", decimals: int = 2) -> str:
    if value is None:
        return "-"

    if isinstance(value, float):
        return f"{value:.{decimals}f} {unit}".strip()

    return f"{value} {unit}".strip()


def bool_dot(value: Any) -> str:
    if value is True:
        return "dot-on"

    if value is False:
        return "dot-off"

    return "dot-unknown"


def percent_to_display(value: Any) -> float:
    if value is None:
        return 0.0

    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0


def number_or_zero(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def int_or_zero(value: Any) -> int:
    return int(round(number_or_zero(value)))


def yes_no(value: Any) -> str:
    return "yes" if value is True else "no"
