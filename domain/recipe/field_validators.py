"""Generic type/field validation helpers - not recipe-specific business rules."""

from __future__ import annotations

import math
from typing import Any


def validate_numeric_field(name: str, value: Any, expected_type: type) -> str | None:
    """
    Rejects everything that is not a genuine, finite number of the expected
    type - returns a clear message instead of letting a bad value reach
    arithmetic further down and raise a raw TypeError/blow up json.dumps.

    bool is deliberately rejected even for expected_type=int:
    isinstance(True, int) is True in Python, so a naive isinstance() check
    would silently accept a checkbox-shaped value as a valid number.

    expected_type=int additionally rejects a non-integral float (e.g. 300.5
    for a mixing-time-in-seconds field) - a whole-number float (300.0) is
    accepted, since JSON does not distinguish "300" from "300.0".
    """
    if isinstance(value, bool):
        return f"'{name}' darf kein Wahrheitswert (true/false) sein (war: {value!r})."

    if not isinstance(value, (int, float)):
        return f"'{name}' muss eine Zahl sein (war: {value!r}, Typ {type(value).__name__})."

    if not math.isfinite(value):
        return f"'{name}' muss eine endliche Zahl sein (war: {value!r})."

    if expected_type is int and isinstance(value, float) and not value.is_integer():
        return f"'{name}' muss eine Ganzzahl sein, keine Kommazahl (war: {value!r})."

    return None


def validate_bool_field(name: str, value: Any) -> str | None:
    """
    Strict boolean check: type(value) is bool, not isinstance() - Python's
    bool is a subtype of int, and isinstance(1, bool) is False but
    isinstance(True, int) is True, so isinstance() alone is asymmetric and
    error-prone here. Never normalizes with bool(value) - a string "false",
    a nonzero number, None, a list, or a dict must all be rejected outright,
    never silently coerced to a truthy/falsy Python bool.
    """
    if type(value) is bool:
        return None

    return f"'{name}' muss ein Wahrheitswert (true/false) sein (war: {value!r}, Typ {type(value).__name__})."


def validate_strict_int_field(name: str, value: Any) -> str | None:
    """
    Strict integer check for structural identifiers (schema_version, slot):
    type(value) is int only, zero float tolerance. Unlike
    validate_numeric_field(..., int), a whole-number float such as 1.0 or
    3.0 is deliberately REJECTED here, not accepted - these two fields are
    never meant to be user-edited/typed-in numbers, only real Python ints
    produced by the code itself. bool is rejected too (bool is a subtype of
    int in Python), and so is any non-numeric type.
    """
    if type(value) is int:
        return None

    return f"'{name}' muss eine echte Ganzzahl sein, ohne Kommastelle (war: {value!r}, Typ {type(value).__name__})."
