"""
nicegui_dashboard/pages/dashboard/recipe_section.py + static/dashboard.css: the
Recipe Editor is a compact dialog (fits a 1280x720 window without scrolling).

Guards the parts of the redesign that must not regress silently:
  - the compact styling is scoped to the editor card only (the Maintenance and
    Prime dialogs share `.recipe-dialog-card` and must keep their look),
  - the long explanations moved into tooltips, but the safety-relevant lines
    (legacy warning/switch, validation errors) stay visible and wired,
  - every UI text in the editor is English (no German labels, no umlauts),
  - the calculated values still update live.

No GPIO/OPC-UA, no browser - NiceGUI elements are built directly, same
tmp-path RECIPE_BOOK_PATH isolation as test_dashboard_page_recipe_editor.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from nicegui import ui

import nicegui_dashboard.recipe_store as recipe_store
from nicegui_dashboard.pages.dashboard.recipe_section import (
    RECIPE_EDITOR_INFO_TEXT,
    VOLUME_PREVIEW_INFO_TEXT,
    build_recipe_dict_from_form,
    build_recipe_section,
    load_recipe_form_from_slot,
    update_recipe_calculations,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = REPO_ROOT / "nicegui_dashboard" / "static" / "dashboard.css"
SECTIONS_DIR = REPO_ROOT / "nicegui_dashboard" / "pages" / "dashboard"

LEGACY_SWITCH_TEXT = "Legacy review completed - I confirm the checked dosing values"
GERMAN_MARKERS = (
    "Grunddaten",
    "Volumenvorschau",
    "Berechnet",
    "Notiz",
    "Speichern",
    "Abbrechen",
    "Rezept",
    "Platzhalter",
    "Warnung",
    "Fehler",
)


@pytest.fixture(autouse=True)
def _isolated_recipe_book(tmp_path, monkeypatch):
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    return tmp_path


def _build_editor(book=None):
    recipe_state = {"book": book if book is not None else recipe_store.default_recipe_book()}
    with ui.column():
        _card_widgets, editor_widgets = build_recipe_section(recipe_state)
    return recipe_state, editor_widgets


def _editor_card(editor_widgets):
    return editor_widgets.recipe_dialog.default_slot.children[0]


def _all_texts(card) -> list[str]:
    texts: list[str] = []
    for element in card.descendants(include_self=True):
        text = getattr(element, "text", None)
        if isinstance(text, str) and text:
            texts.append(text)
        for prop in ("label", "suffix", "prefix"):
            value = element._props.get(prop)
            if isinstance(value, str) and value:
                texts.append(value)
    return texts


def _css_selectors(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    selectors: list[str] = []
    for prelude in re.findall(r"([^{}]+)\{", css):
        prelude = prelude.strip()
        if prelude.startswith("@"):
            continue
        selectors.extend(part.strip() for part in prelude.split(","))
    return selectors


def test_compact_styling_is_scoped_to_the_recipe_editor_card():
    _state, widgets = _build_editor()
    classes = _editor_card(widgets).classes

    assert "recipe-dialog-card" in classes
    assert "recipe-editor-card" in classes

    # Maintenance / Prime reuse .recipe-dialog-card and must not pick up the
    # compact overrides.
    for module in ("maintenance_section.py", "prime_section.py"):
        assert "recipe-editor-card" not in (SECTIONS_DIR / module).read_text(encoding="utf-8")


def test_compact_css_block_only_contains_rules_scoped_to_the_editor_card():
    css = CSS_PATH.read_text(encoding="utf-8")
    start = css.rindex("/*", 0, css.index("Recipe editor - compact layout"))
    end = css.rindex("/*", 0, css.index("Process control / Manual Drain Jog"))
    assert start < end

    selectors = _css_selectors(css[start:end])

    assert selectors, "compact layout block is empty"
    unscoped = [selector for selector in selectors if "recipe-editor-card" not in selector]
    assert unscoped == []


def test_long_explanations_are_tooltips_not_visible_labels():
    _state, widgets = _build_editor()
    card = _editor_card(widgets)

    tooltip_texts = [e.text for e in card.descendants() if isinstance(e, ui.tooltip)]
    label_texts = [e.text for e in card.descendants() if isinstance(e, ui.label)]

    assert RECIPE_EDITOR_INFO_TEXT in tooltip_texts
    assert VOLUME_PREVIEW_INFO_TEXT in tooltip_texts
    assert RECIPE_EDITOR_INFO_TEXT not in label_texts
    assert VOLUME_PREVIEW_INFO_TEXT not in label_texts

    # Wording itself is unchanged by the layout work.
    assert "loading a recipe never activates it" in RECIPE_EDITOR_INFO_TEXT
    assert "no peristaltic-pump consumer" in RECIPE_EDITOR_INFO_TEXT
    assert "demand-driven EC correction is not implemented yet" in VOLUME_PREVIEW_INFO_TEXT


def test_safety_relevant_lines_stay_visible_and_are_not_tooltips():
    _state, widgets = _build_editor()
    card = _editor_card(widgets)
    tooltip_descendant_ids = {
        d.id
        for e in card.descendants()
        if isinstance(e, ui.tooltip)
        for d in e.descendants(include_self=True)
    }

    for label in (widgets.recipe_legacy_warning_label, widgets.recipe_validation_error_label):
        assert "error-text" in label.classes
        assert label.id not in tooltip_descendant_ids
        assert label in list(card.descendants())

    assert widgets.legacy_review_confirmed_switch.text == LEGACY_SWITCH_TEXT
    assert widgets.legacy_review_confirmed_switch.id not in tooltip_descendant_ids
    assert any(
        isinstance(e, ui.label) and e.text.startswith("pH correction is not yet included")
        for e in card.descendants()
    )


def test_recipe_editor_texts_are_english():
    _state, widgets = _build_editor()
    texts = _all_texts(_editor_card(widgets))

    assert "Volume preview" in texts
    assert "Calculated for this run" in texts
    assert "Recipe Editor" in texts
    assert "Save Recipe" in texts

    for text in texts:
        assert not re.search("[äöüÄÖÜß]", text), text
        for marker in GERMAN_MARKERS:
            assert marker not in text, f"German text in recipe editor: {text!r}"


def test_calculated_values_are_chips_and_still_update_live():
    state, widgets = _build_editor()
    form_state: dict = {}

    load_recipe_form_from_slot(state, form_state, widgets)

    chips = (
        widgets.calc_total_nutrients_label,
        widgets.calc_nutrient_a_label,
        widgets.calc_nutrient_b_label,
        widgets.calc_process_volume_label,
        widgets.calc_remaining_capacity_label,
        widgets.calc_max_possible_ro_correction_label,
    )
    for chip in chips:
        assert "recipe-chip" in chip.classes
    assert widgets.calc_total_nutrients_label.text.startswith("Total dose: ")
    assert widgets.calc_nutrient_a_label.text.startswith("Amount A: ")
    assert widgets.calc_nutrient_b_label.text.startswith("Amount B: ")
    assert widgets.calc_process_volume_label.text.startswith("Estimated recipe volume: ")
    assert widgets.calc_remaining_capacity_label.text.startswith("Remaining capacity to 185 L: ")
    assert widgets.calc_max_possible_ro_correction_label.text.startswith(
        "Max. possible later RO correction: "
    )

    widgets.nutrient_dosing_enabled_switch.value = True
    widgets.nutrients_dose_input.value = 10
    widgets.nutrient_a_percent_input.value = 25
    widgets.target_ro_water_input.value = 100
    update_recipe_calculations(widgets, form_state)

    assert widgets.calc_total_nutrients_label.text == "Total dose: 10.000 ml"
    assert widgets.calc_nutrient_a_label.text == "Amount A: 2.500 ml"
    assert widgets.calc_nutrient_b_label.text == "Amount B: 7.500 ml"
    assert widgets.nutrient_b_percent_display.value == 75


def test_validation_error_label_still_reports_invalid_values():
    state, widgets = _build_editor()
    form_state: dict = {}
    load_recipe_form_from_slot(state, form_state, widgets)
    assert widgets.recipe_validation_error_label.text == ""

    widgets.target_ec_input.value = 99
    update_recipe_calculations(widgets, form_state)

    assert widgets.recipe_validation_error_label.text != ""


def test_legacy_review_stays_flagged_until_switch_is_confirmed():
    book = recipe_store.default_recipe_book()
    book["favorites"][0]["legacy_dosing_needs_review"] = True
    book["favorites"][0]["legacy_volume_stock_1_ml"] = 12.0
    book["favorites"][0]["legacy_volume_stock_2_ml"] = 8.0
    state, widgets = _build_editor(book)
    form_state: dict = {}

    load_recipe_form_from_slot(state, form_state, widgets)

    assert widgets.legacy_review_confirmed_switch.value is False
    assert "need manual review" in widgets.recipe_legacy_warning_label.text
    # Fail closed: without the explicit confirmation the recipe stays flagged.
    assert build_recipe_dict_from_form(widgets, form_state, 1)["legacy_dosing_needs_review"] is True

    widgets.legacy_review_confirmed_switch.value = True
    assert build_recipe_dict_from_form(widgets, form_state, 1)["legacy_dosing_needs_review"] is False
