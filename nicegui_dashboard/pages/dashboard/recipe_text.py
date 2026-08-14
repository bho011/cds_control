"""Textzusammenfassungen eines Rezepts für die Recipe-Karte im Dashboard."""

from __future__ import annotations

from typing import Any

from nicegui_dashboard.recipe_store import get_recipe_preview


def recipe_summary(recipe: dict[str, Any]) -> str:
    return (
        f"Tank={recipe.get('target_tank', '-')} | "
        f"RO Water={recipe.get('target_ro_water_l', '-')} L | "
        f"EC={recipe.get('target_ec_ms_cm', '-')} mS/cm | "
        f"pH={recipe.get('target_ph', '-')}"
    )


def dosing_summary(recipe: dict[str, Any]) -> str:
    try:
        preview = get_recipe_preview(recipe)
        nutrients_part = (
            f"Nutrients={preview.total_nutrients_ml:.1f} ml "
            f"(A={preview.nutrient_a_ml:.1f} / B={preview.nutrient_b_ml:.1f})"
        )
    except Exception:
        nutrients_part = "Nutrients=-"

    return (
        f"{nutrients_part} | "
        f"Acid1={recipe.get('volume_acid_1_ml', '-')} ml | "
        f"Acid2={recipe.get('volume_acid_2_ml', '-')} ml | "
        f"Base={recipe.get('volume_base_ml', '-')} ml"
    )


def volume_summary(recipe: dict[str, Any]) -> str:
    try:
        preview = get_recipe_preview(recipe)
        return (
            f"Est. recipe volume={preview.estimated_process_volume_l:.3f} L "
            f"(remaining to 185 L={preview.remaining_capacity_l:.3f} L) | "
            f"Max. possible later RO correction={preview.max_possible_ro_correction_l:.3f} L"
        )
    except Exception:
        return "Est. recipe volume=-"
