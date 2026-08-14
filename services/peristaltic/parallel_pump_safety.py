"""Parallel-Pumpen-Beschränkung (pair-test / all-four-test)."""

from __future__ import annotations

ALLOWED_PARALLEL_PUMP_GROUPS: dict[str, list[frozenset[str]]] = {
    # Keine Paralleltests auf MCU_A - pH-Säure/Base dürfen nie gleichzeitig
    # dosieren (siehe docs/CDS_EC_MIXING_PROCESS_CONCEPT.md).
    "MCU_A": [],
    "MCU_B": [
        # Physische Hardwareanordnung: Nährstoff A = P1+P3, Nährstoff B =
        # P2+P4 (siehe config/peristaltic_mapping.json - P1=nutrient_a_1,
        # P2=nutrient_b_1, P3=nutrient_a_2, P4=nutrient_b_2).
        frozenset({"P1", "P3"}),
        frozenset({"P2", "P4"}),
        frozenset({"P1", "P2", "P3", "P4"}),
    ],
}


def validate_parallel_pump_selection(controller: str, pumps: list[str]) -> list[str]:
    """Sammelt Fehler: die übergebene Pumpenmenge muss exakt einer der für
    diesen Controller erlaubten Gruppen entsprechen. MCU_A erlaubt gar
    keine Paralleltests. MCU_B erlaubt nur {P1,P3} und {P2,P4} (pair-test,
    je ein Pumpenpaar pro Nährstofflösung) oder alle vier (all-four-test) -
    explizit NICHT {P1,P2}, {P3,P4} oder sonstige Kombinationen."""
    allowed_groups = ALLOWED_PARALLEL_PUMP_GROUPS.get(controller)
    if allowed_groups is None:
        return [f"Unbekannter Controller für Paralleltest: {controller!r}."]

    requested_group = frozenset(pumps)

    if requested_group in allowed_groups:
        return []

    if not allowed_groups:
        return [f"Paralleltests sind für {controller} nicht zulässig (nur Einzeltest/-kalibrierung)."]

    allowed_display = " oder ".join("{" + ",".join(sorted(g)) + "}" for g in allowed_groups)
    return [
        f"Pumpenkombination {{{','.join(sorted(requested_group))}}} ist für {controller} nicht zulässig "
        f"- erlaubt ist nur {allowed_display}."
    ]
