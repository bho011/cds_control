"""Recipe-Karte (aktives Rezept) + Recipe-Editor-Dialog (3 Favoriten, EC/pH-Formular)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from nicegui import ui

from domain.recipe_limits import RecipeValidationError, nutrient_b_percent
from domain.recipe_model import StoredRecipe
from nicegui_dashboard.components.formatting import int_or_zero, number_or_zero, yes_no
from nicegui_dashboard.pages.dashboard.recipe_text import dosing_summary, recipe_summary, volume_summary
from nicegui_dashboard.recipe_store import (
    RecipeBookCorruptedError,
    get_active_recipe,
    get_recipe_by_slot,
    get_recipe_preview,
    load_recipe_book,
    save_recipe_to_slot,
    set_active_slot,
)


@dataclass
class RecipeCardWidgets:
    recipe_name_label: ui.label
    recipe_summary_label: ui.label
    recipe_dosing_label: ui.label
    recipe_process_label: ui.label
    recipe_volume_label: ui.label
    favorite_badges: dict[int, ui.button]
    edit_recipe_button: ui.button
    apply_recipe_button: ui.button


@dataclass
class RecipeEditorWidgets:
    recipe_dialog: ui.dialog
    recipe_slot_select: ui.select
    recipe_make_active_switch: ui.switch
    recipe_name_input: ui.input
    target_tank_input: ui.input
    target_ro_water_input: ui.number
    recipe_legacy_warning_label: ui.label
    legacy_review_confirmed_switch: ui.switch
    target_ec_input: ui.number
    ec_mixing_time_input: ui.number
    nutrient_dosing_enabled_switch: ui.switch
    nutrients_dose_input: ui.number
    nutrient_a_percent_input: ui.number
    nutrient_b_percent_display: ui.number
    ec_adjustment_factor_input: ui.number
    calc_total_nutrients_label: ui.label
    calc_nutrient_a_label: ui.label
    calc_nutrient_b_label: ui.label
    target_ph_input: ui.number
    volume_acid_1_input: ui.number
    volume_acid_2_input: ui.number
    volume_base_input: ui.number
    ph_mixing_time_input: ui.number
    ph_adjustment_factor_input: ui.number
    calc_process_volume_label: ui.label
    calc_remaining_capacity_label: ui.label
    calc_max_possible_ro_correction_label: ui.label
    recipe_validation_error_label: ui.label
    recipe_notes_input: ui.textarea
    save_recipe_button: ui.button


def build_recipe_section(recipe_state: dict[str, Any]) -> tuple[RecipeCardWidgets, RecipeEditorWidgets]:
    """Builds the Recipe card and its Editor dialog together, in one nested
    block - matches the pre-split structure exactly (the dialog is created
    while still inside the card's `with` block)."""
    with ui.card().classes("panel recipe-panel"):
        ui.label("Recipe / Setpoints").classes("panel-title")
        ui.label("Active recipe from recipes/dashboard_recipes.json").classes(
            "panel-subtitle"
        )

        recipe_name_label = ui.label("Recipe: -").classes("recipe-name")
        recipe_summary_label = ui.label("-").classes("recipe-summary")
        recipe_dosing_label = ui.label("-").classes("recipe-detail")
        recipe_process_label = ui.label("-").classes("recipe-detail")
        recipe_volume_label = ui.label("-").classes("recipe-detail")

        with ui.row().classes("recipe-favorites-row"):
            favorite_badges = {
                1: ui.button("F1").classes("recipe-favorite-badge"),
                2: ui.button("F2").classes("recipe-favorite-badge"),
                3: ui.button("F3").classes("recipe-favorite-badge"),
            }
            for _badge in favorite_badges.values():
                _badge.props("flat no-caps")

        with ui.row().classes("w-full gap-3 mt-3"):
            edit_recipe_button = ui.button("Edit Recipe").classes(
                "flex-1 font-bold"
            ).props("color=primary")
            apply_recipe_button = ui.button("Reload Active Recipe").classes(
                "flex-1 font-bold"
            ).props("color=secondary outline")

        with ui.dialog() as recipe_dialog, ui.card().classes("recipe-dialog-card"):
            ui.label("Recipe Editor").classes("panel-title")
            ui.label(
                "Three favorites can be saved. Fill volume genuinely drives the next run. "
                "Sensor circulation is a per-run choice made in Process Control, not part of "
                "the recipe - loading a recipe never activates it. EC nutrient dosing is "
                "calculated and validated here but still has no peristaltic-pump consumer "
                "yet (see README.md Section 15/17)."
            ).classes("panel-subtitle")

            with ui.row().classes("w-full gap-3"):
                recipe_slot_select = ui.select(
                    options=[1, 2, 3],
                    value=int(recipe_state["book"].get("active_slot", 1)),
                    label="Favorite Slot",
                ).classes("control-input flex-1")
                recipe_make_active_switch = ui.switch(
                    "Set as active recipe",
                    value=True,
                ).classes("text-slate-200")

            ui.label("Grunddaten").classes("recipe-section-title")
            with ui.element("div").classes("recipe-top-grid"):
                recipe_name_input = ui.input("Recipe Name").classes(
                    "control-input"
                )
                target_tank_input = ui.input("Tank Selection").classes(
                    "control-input"
                )
                target_ro_water_input = ui.number(
                    "RO Water Volume", suffix="L", min=0, max=180, step=1
                ).classes("control-input")

            recipe_legacy_warning_label = ui.label("").classes("error-text")
            legacy_review_confirmed_switch = ui.switch(
                "Legacy review completed - I confirm the checked dosing values"
            ).classes("text-slate-200")

            with ui.element("div").classes("recipe-form-grid"):
                with ui.column().classes("recipe-form-section"):
                    ui.label("EC").classes("recipe-section-title")
                    target_ec_input = ui.number(
                        "EC Setpoint", suffix="mS/cm", min=0.5, max=2.5, step=0.05
                    )
                    ec_mixing_time_input = ui.number(
                        "Mixing time", suffix="s (seconds)", min=300, max=1200, step=10
                    )
                    nutrient_dosing_enabled_switch = ui.switch(
                        "Enable nutrient dosing"
                    )
                    nutrients_dose_input = ui.number(
                        "Nutrient total dose", suffix="ml / 100 L RO water", min=0, step=1
                    )
                    nutrient_a_percent_input = ui.number(
                        "Nutrient solution A share", suffix="%", min=0, max=100, step=1
                    )
                    nutrient_b_percent_display = ui.number(
                        "Nutrient solution B share (auto)", suffix="%"
                    ).props("readonly")
                    ec_adjustment_factor_input = ui.number(
                        "EC adjustment factor", min=0.01, max=1, step=0.01
                    )

                    ui.label("Calculated for this run").classes(
                        "recipe-section-title mt-2"
                    )
                    calc_total_nutrients_label = ui.label("Total dose: - ml")
                    calc_nutrient_a_label = ui.label("Amount A: - ml")
                    calc_nutrient_b_label = ui.label("Amount B: - ml")

                with ui.column().classes("recipe-form-section"):
                    ui.label("pH").classes("recipe-section-title")
                    target_ph_input = ui.number("pH Setpoint", min=0, max=14, step=0.1)
                    volume_acid_1_input = ui.number(
                        "Volume acid 1", suffix="ml", min=0, step=1
                    )
                    volume_acid_2_input = ui.number(
                        "Volume acid 2", suffix="ml", min=0, step=1
                    )
                    volume_base_input = ui.number(
                        "Volume base", suffix="ml", min=0, step=1
                    )
                    ph_mixing_time_input = ui.number(
                        "Mixing time", suffix="s", min=0, step=10
                    )
                    ph_adjustment_factor_input = ui.number(
                        "Adjustment factor", min=0, step=0.01
                    )
                    ui.label(
                        "pH correction is not yet included in the process-volume "
                        "estimate - the exact amount is not known at recipe time."
                    ).classes("confirmation-help")

            ui.label("Volumenvorschau").classes("recipe-section-title mt-2")
            with ui.element("div").classes("recipe-top-grid"):
                calc_process_volume_label = ui.label("Estimated recipe volume: - L")
                calc_remaining_capacity_label = ui.label("Remaining capacity to 185 L: - L")
                calc_max_possible_ro_correction_label = ui.label(
                    "Max. possible later RO correction: - L"
                )
            ui.label(
                "A fixed RO correction is not part of a recipe - this is only an estimate "
                "of how much correction capacity would technically remain. A real, "
                "demand-driven EC correction is not implemented yet."
            ).classes("confirmation-help")

            recipe_validation_error_label = ui.label("").classes("error-text")

            recipe_notes_input = ui.textarea("Note").classes(
                "control-input w-full"
            )

            with ui.row().classes("w-full justify-end gap-3"):
                ui.button("Cancel", on_click=recipe_dialog.close).props(
                    "color=grey outline"
                )
                save_recipe_button = ui.button("Save Recipe").props(
                    "color=positive"
                )

    card_widgets = RecipeCardWidgets(
        recipe_name_label=recipe_name_label,
        recipe_summary_label=recipe_summary_label,
        recipe_dosing_label=recipe_dosing_label,
        recipe_process_label=recipe_process_label,
        recipe_volume_label=recipe_volume_label,
        favorite_badges=favorite_badges,
        edit_recipe_button=edit_recipe_button,
        apply_recipe_button=apply_recipe_button,
    )

    editor_widgets = RecipeEditorWidgets(
        recipe_dialog=recipe_dialog,
        recipe_slot_select=recipe_slot_select,
        recipe_make_active_switch=recipe_make_active_switch,
        recipe_name_input=recipe_name_input,
        target_tank_input=target_tank_input,
        target_ro_water_input=target_ro_water_input,
        recipe_legacy_warning_label=recipe_legacy_warning_label,
        legacy_review_confirmed_switch=legacy_review_confirmed_switch,
        target_ec_input=target_ec_input,
        ec_mixing_time_input=ec_mixing_time_input,
        nutrient_dosing_enabled_switch=nutrient_dosing_enabled_switch,
        nutrients_dose_input=nutrients_dose_input,
        nutrient_a_percent_input=nutrient_a_percent_input,
        nutrient_b_percent_display=nutrient_b_percent_display,
        ec_adjustment_factor_input=ec_adjustment_factor_input,
        calc_total_nutrients_label=calc_total_nutrients_label,
        calc_nutrient_a_label=calc_nutrient_a_label,
        calc_nutrient_b_label=calc_nutrient_b_label,
        target_ph_input=target_ph_input,
        volume_acid_1_input=volume_acid_1_input,
        volume_acid_2_input=volume_acid_2_input,
        volume_base_input=volume_base_input,
        ph_mixing_time_input=ph_mixing_time_input,
        ph_adjustment_factor_input=ph_adjustment_factor_input,
        calc_process_volume_label=calc_process_volume_label,
        calc_remaining_capacity_label=calc_remaining_capacity_label,
        calc_max_possible_ro_correction_label=calc_max_possible_ro_correction_label,
        recipe_validation_error_label=recipe_validation_error_label,
        recipe_notes_input=recipe_notes_input,
        save_recipe_button=save_recipe_button,
    )

    return card_widgets, editor_widgets


def update_recipe_card(recipe_state: dict[str, Any], card_widgets: RecipeCardWidgets) -> None:
    recipe_book = recipe_state["book"]
    active_slot = int(recipe_book.get("active_slot", 1))
    active_recipe = get_active_recipe(recipe_book)

    card_widgets.recipe_name_label.set_text(
        f"Favorite {active_slot}: {active_recipe.get('recipe_name', '-')}"
    )
    card_widgets.recipe_summary_label.set_text(recipe_summary(active_recipe))
    card_widgets.recipe_dosing_label.set_text(dosing_summary(active_recipe))
    card_widgets.recipe_process_label.set_text(
        f"Nutrient dosing enabled={yes_no(active_recipe.get('nutrient_dosing_enabled'))} | "
        f"Legacy review needed={yes_no(active_recipe.get('legacy_dosing_needs_review'))}"
    )
    card_widgets.recipe_volume_label.set_text(volume_summary(active_recipe))

    for slot, badge in card_widgets.favorite_badges.items():
        recipe = get_recipe_by_slot(recipe_book, slot)
        badge.set_text(f"F{slot}: {recipe.get('recipe_name', '-')}")
        badge.classes(remove="recipe-favorite-active")
        if slot == active_slot:
            badge.classes("recipe-favorite-active")


def build_recipe_dict_from_form(
    editor_widgets: RecipeEditorWidgets, recipe_form_state: dict[str, Any], slot: int
) -> dict[str, Any]:
    return {
        "recipe_name": editor_widgets.recipe_name_input.value or f"Favorit {slot}",
        "target_tank": editor_widgets.target_tank_input.value or "Ch 1 - T1",
        "target_ro_water_l": number_or_zero(editor_widgets.target_ro_water_input.value),

        "target_ec_ms_cm": number_or_zero(editor_widgets.target_ec_input.value),
        "ec_mixing_time_seconds": int_or_zero(editor_widgets.ec_mixing_time_input.value),
        "nutrient_dosing_enabled": bool(editor_widgets.nutrient_dosing_enabled_switch.value),
        "nutrients_ml_per_100_l": number_or_zero(editor_widgets.nutrients_dose_input.value),
        "nutrient_a_percent": number_or_zero(editor_widgets.nutrient_a_percent_input.value),
        "ec_adjustment_factor": number_or_zero(editor_widgets.ec_adjustment_factor_input.value),

        "target_ph": number_or_zero(editor_widgets.target_ph_input.value),
        "volume_acid_1_ml": number_or_zero(editor_widgets.volume_acid_1_input.value),
        "volume_acid_2_ml": number_or_zero(editor_widgets.volume_acid_2_input.value),
        "volume_base_ml": number_or_zero(editor_widgets.volume_base_input.value),
        "ph_mixing_time_seconds": int_or_zero(editor_widgets.ph_mixing_time_input.value),
        "ph_adjustment_factor": number_or_zero(editor_widgets.ph_adjustment_factor_input.value),

        "notes": editor_widgets.recipe_notes_input.value or "",

        "legacy_volume_stock_1_ml": recipe_form_state.get("legacy_volume_stock_1_ml"),
        "legacy_volume_stock_2_ml": recipe_form_state.get("legacy_volume_stock_2_ml"),
        # The "Legacy review completed" switch is the ONLY way this flag
        # can go from true to false - it never clears itself just
        # because some number was typed (see docs/OPEN_RECIPE_DECISIONS.md).
        "legacy_dosing_needs_review": not bool(editor_widgets.legacy_review_confirmed_switch.value),
    }


def update_recipe_calculations(
    editor_widgets: RecipeEditorWidgets, recipe_form_state: dict[str, Any]
) -> None:
    """Live recompute, called on every relevant field change - B always
    follows A, and every calculated/estimated value updates immediately.
    Deliberately never clamps or blocks typing; only the red error label
    and the Save button's behavior enforce the actual limits."""
    slot = int(editor_widgets.recipe_slot_select.value or 1)
    raw_recipe = build_recipe_dict_from_form(editor_widgets, recipe_form_state, slot)

    editor_widgets.nutrient_b_percent_display.value = nutrient_b_percent(
        number_or_zero(editor_widgets.nutrient_a_percent_input.value)
    )

    try:
        preview = get_recipe_preview(raw_recipe)
    except Exception:
        preview = None

    if preview is not None:
        editor_widgets.calc_total_nutrients_label.set_text(
            f"Total dose: {preview.total_nutrients_ml:.3f} ml"
        )
        editor_widgets.calc_nutrient_a_label.set_text(f"Amount A: {preview.nutrient_a_ml:.3f} ml")
        editor_widgets.calc_nutrient_b_label.set_text(f"Amount B: {preview.nutrient_b_ml:.3f} ml")
        editor_widgets.calc_process_volume_label.set_text(
            f"Estimated recipe volume: {preview.estimated_process_volume_l:.3f} L"
        )
        editor_widgets.calc_remaining_capacity_label.set_text(
            f"Remaining capacity to 185 L: {preview.remaining_capacity_l:.3f} L"
        )
        # Purely informational - no fixed RO correction is part of a
        # recipe or RunConfigSnapshot anymore, see
        # domain/recipe_limits.py::compute_max_possible_ro_correction_l.
        editor_widgets.calc_max_possible_ro_correction_label.set_text(
            f"Max. possible later RO correction: {preview.max_possible_ro_correction_l:.3f} L"
        )

    errors = StoredRecipe.from_dict(raw_recipe).validate()
    editor_widgets.recipe_validation_error_label.set_text("\n".join(errors))


def load_recipe_form_from_slot(
    recipe_state: dict[str, Any],
    recipe_form_state: dict[str, Any],
    editor_widgets: RecipeEditorWidgets,
) -> None:
    recipe_book = recipe_state["book"]
    slot = int(editor_widgets.recipe_slot_select.value or 1)
    recipe = get_recipe_by_slot(recipe_book, slot)

    editor_widgets.recipe_name_input.value = recipe.get("recipe_name", "")
    editor_widgets.target_tank_input.value = recipe.get("target_tank", "")
    editor_widgets.target_ro_water_input.value = recipe.get("target_ro_water_l", 0)

    editor_widgets.target_ec_input.value = recipe.get("target_ec_ms_cm", 0)
    editor_widgets.ec_mixing_time_input.value = recipe.get("ec_mixing_time_seconds", 0)
    editor_widgets.nutrient_dosing_enabled_switch.value = bool(
        recipe.get("nutrient_dosing_enabled", False)
    )
    editor_widgets.nutrients_dose_input.value = recipe.get("nutrients_ml_per_100_l", 0)
    editor_widgets.nutrient_a_percent_input.value = recipe.get("nutrient_a_percent", 50)
    editor_widgets.ec_adjustment_factor_input.value = recipe.get("ec_adjustment_factor", 0)

    editor_widgets.target_ph_input.value = recipe.get("target_ph", 0)
    editor_widgets.volume_acid_1_input.value = recipe.get("volume_acid_1_ml", 0)
    editor_widgets.volume_acid_2_input.value = recipe.get("volume_acid_2_ml", 0)
    editor_widgets.volume_base_input.value = recipe.get("volume_base_ml", 0)
    editor_widgets.ph_mixing_time_input.value = recipe.get("ph_mixing_time_seconds", 0)
    editor_widgets.ph_adjustment_factor_input.value = recipe.get("ph_adjustment_factor", 0)

    editor_widgets.recipe_notes_input.value = recipe.get("notes", "")

    recipe_form_state["legacy_volume_stock_1_ml"] = recipe.get("legacy_volume_stock_1_ml")
    recipe_form_state["legacy_volume_stock_2_ml"] = recipe.get("legacy_volume_stock_2_ml")
    recipe_form_state["legacy_dosing_needs_review"] = bool(
        recipe.get("legacy_dosing_needs_review", False)
    )
    editor_widgets.legacy_review_confirmed_switch.value = not recipe_form_state[
        "legacy_dosing_needs_review"
    ]

    if recipe_form_state["legacy_dosing_needs_review"]:
        editor_widgets.recipe_legacy_warning_label.set_text(
            "Legacy EC stock volumes need manual review (not auto-converted - the "
            "reference water amount they were sized for is not reliably known): "
            f"Stock 1={recipe_form_state['legacy_volume_stock_1_ml']} ml, "
            f"Stock 2={recipe_form_state['legacy_volume_stock_2_ml']} ml. "
            "Nutrient total dose defaults to 0 ml/100L until a confirmed value is entered."
        )
    else:
        editor_widgets.recipe_legacy_warning_label.set_text("")

    update_recipe_calculations(editor_widgets, recipe_form_state)


def open_recipe_dialog(
    recipe_state: dict[str, Any],
    recipe_form_state: dict[str, Any],
    editor_widgets: RecipeEditorWidgets,
    add_log: Callable[[str], None],
) -> None:
    try:
        recipe_state["book"] = load_recipe_book()
    except RecipeBookCorruptedError as exc:
        ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
        add_log(f"[RECIPE] {exc}")
        return

    editor_widgets.recipe_slot_select.value = int(recipe_state["book"].get("active_slot", 1))
    editor_widgets.recipe_make_active_switch.value = True
    load_recipe_form_from_slot(recipe_state, recipe_form_state, editor_widgets)
    editor_widgets.recipe_dialog.open()


def handle_apply_active_recipe(
    recipe_state: dict[str, Any],
    card_widgets: RecipeCardWidgets,
    add_log: Callable[[str], None],
) -> None:
    try:
        recipe_state["book"] = load_recipe_book()
    except RecipeBookCorruptedError as exc:
        ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
        add_log(f"[RECIPE] {exc}")
        return

    update_recipe_card(recipe_state, card_widgets)
    active_recipe = get_active_recipe(recipe_state["book"])
    ui.notify(
        f"Active recipe loaded: {active_recipe.get('recipe_name', '-')}",
        color="positive",
    )
    add_log(f"[RECIPE] Active recipe: {active_recipe.get('recipe_name', '-')}")


def handle_save_recipe(
    recipe_state: dict[str, Any],
    recipe_form_state: dict[str, Any],
    card_widgets: RecipeCardWidgets,
    editor_widgets: RecipeEditorWidgets,
    add_log: Callable[[str], None],
) -> None:
    slot = int(editor_widgets.recipe_slot_select.value or 1)
    recipe = build_recipe_dict_from_form(editor_widgets, recipe_form_state, slot)

    try:
        recipe_state["book"] = save_recipe_to_slot(
            slot=slot,
            recipe=recipe,
            make_active=bool(editor_widgets.recipe_make_active_switch.value),
        )
    except RecipeValidationError as exc:
        editor_widgets.recipe_validation_error_label.set_text(str(exc))
        ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
        add_log(f"[RECIPE] Save blocked (Slot {slot}): validation failed.")
        return

    update_recipe_card(recipe_state, card_widgets)
    editor_widgets.recipe_dialog.close()
    ui.notify("Recipe saved.", color="positive")
    add_log(f"[RECIPE] Favorite {slot} saved: {recipe['recipe_name']}")


def handle_favorite_click(
    slot: int,
    recipe_state: dict[str, Any],
    card_widgets: RecipeCardWidgets,
    add_log: Callable[[str], None],
) -> None:
    """F1/F2/F3 click: only changes which favorite is active - never
    starts a process, never touches hardware. Strict ordering: set the
    active slot (which itself loads fresh, validates, and saves
    atomically under lock) FIRST, only then update recipe_state and the
    UI - so update_recipe_card() never runs against a stale book."""
    try:
        fresh_book = set_active_slot(slot)
    except (RecipeBookCorruptedError, ValueError) as exc:
        ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
        add_log(f"[RECIPE] Favorite {slot} activation blocked: {exc}")
        return

    recipe_state["book"] = fresh_book
    update_recipe_card(recipe_state, card_widgets)

    active_recipe = get_recipe_by_slot(recipe_state["book"], slot)
    ui.notify(f"Active recipe: {active_recipe.get('recipe_name', '-')}", color="positive")
    add_log(f"[RECIPE] Favorite {slot} activated: {active_recipe.get('recipe_name', '-')}")


def wire_recipe_handlers(
    recipe_state: dict[str, Any],
    recipe_form_state: dict[str, Any],
    card_widgets: RecipeCardWidgets,
    editor_widgets: RecipeEditorWidgets,
    add_log: Callable[[str], None],
) -> None:
    for slot, badge in card_widgets.favorite_badges.items():
        badge.on_click(lambda _, slot=slot: handle_favorite_click(slot, recipe_state, card_widgets, add_log))

    card_widgets.edit_recipe_button.on_click(
        lambda: open_recipe_dialog(recipe_state, recipe_form_state, editor_widgets, add_log)
    )
    card_widgets.apply_recipe_button.on_click(
        lambda: handle_apply_active_recipe(recipe_state, card_widgets, add_log)
    )
    editor_widgets.save_recipe_button.on_click(
        lambda: handle_save_recipe(recipe_state, recipe_form_state, card_widgets, editor_widgets, add_log)
    )
    editor_widgets.recipe_slot_select.on_value_change(
        lambda _: load_recipe_form_from_slot(recipe_state, recipe_form_state, editor_widgets)
    )

    for calc_input in (
        editor_widgets.target_ro_water_input,
        editor_widgets.nutrient_dosing_enabled_switch,
        editor_widgets.nutrients_dose_input,
        editor_widgets.nutrient_a_percent_input,
        editor_widgets.ec_adjustment_factor_input,
        editor_widgets.legacy_review_confirmed_switch,
    ):
        calc_input.on_value_change(lambda _: update_recipe_calculations(editor_widgets, recipe_form_state))
