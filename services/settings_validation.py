"""
Shared settings-file validation, following the raise-on-invalid style already
used by process/auto_circulation.py's load_auto_circulation_config() and
AutoCirculationController.__init__ - generalized so every *_settings.json
loader can use the same mechanism instead of relying on scattered
settings.get(key, default) calls whose code-level default can silently
diverge from what the JSON file actually says (see the
Architecture-Hardening-Roadmap plan, Phase 3, for the concrete bug this
closes).

No new dependency (no pydantic/jsonschema) - deliberately plain, matching
this project's existing minimal-dependency style.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class SettingsValidationError(ValueError):
    """Raised with every problem found in a settings dict, not just the first."""


@dataclass
class SettingField:
    name: str
    type: type  # float | int | str | bool | list
    required: bool = True
    default: Any = None
    min_value: float | None = None
    max_value: float | None = None
    allowed_values: set | None = None
    # Custom check run after type/range checks pass. Returns an error
    # message to reject the value, or None to accept it.
    validator: Callable[[Any], str | None] | None = None


def validate_settings(settings: dict, schema: list[SettingField], file_label: str) -> dict:
    """
    Returns a validated, type-coerced copy of `settings` (missing optional
    keys filled in with their default). Raises SettingsValidationError
    listing every problem found if any required key is missing, has the
    wrong type, or fails a range/allowed-values/custom check - so a settings
    file with several problems reports all of them in one go, not one
    fix-rerun-find-the-next-one cycle at a time.

    Keys present in `settings` but not mentioned in `schema` are passed
    through unchanged and are not an error - this validates that required
    configuration is present and sane, it does not enforce a closed set of
    allowed keys.
    """
    errors: list[str] = []
    result = dict(settings)

    for spec in schema:
        if spec.name not in settings:
            if spec.required:
                errors.append(f"'{spec.name}' fehlt.")
            else:
                result[spec.name] = spec.default
            continue

        raw_value = settings[spec.name]

        if spec.type is list:
            if not isinstance(raw_value, list):
                errors.append(f"'{spec.name}' muss eine Liste sein (war: {raw_value!r}).")
                continue
            value: Any = raw_value
        else:
            try:
                value = bool(raw_value) if spec.type is bool else spec.type(raw_value)
            except (TypeError, ValueError):
                errors.append(
                    f"'{spec.name}' muss vom Typ {spec.type.__name__} sein (war: {raw_value!r})."
                )
                continue

        if spec.min_value is not None and value < spec.min_value:
            errors.append(f"'{spec.name}' muss >= {spec.min_value} sein (war: {value}).")
            continue

        if spec.max_value is not None and value > spec.max_value:
            errors.append(f"'{spec.name}' muss <= {spec.max_value} sein (war: {value}).")
            continue

        if spec.allowed_values is not None and value not in spec.allowed_values:
            errors.append(
                f"'{spec.name}' muss einer von {sorted(spec.allowed_values)} sein (war: {value!r})."
            )
            continue

        if spec.validator is not None:
            message = spec.validator(value)
            if message:
                errors.append(f"'{spec.name}': {message}")
                continue

        result[spec.name] = value

    if errors:
        formatted = "\n".join(f"  - {message}" for message in errors)
        raise SettingsValidationError(
            f"{file_label}: {len(errors)} Fehler in den Einstellungen:\n{formatted}"
        )

    return result
