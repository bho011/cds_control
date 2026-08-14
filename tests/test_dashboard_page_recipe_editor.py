"""
nicegui_dashboard/pages/dashboard_page.py: the Recipe Editor's numeric fields
must each carry a field-appropriate HTML `step` (drives the browser's native
+/- spin buttons) - without it, the browser falls back to step=1 for every
field, which is wrong for e.g. factors (0.01) or pH (0.1). No GPIO/OPC-UA,
no browser - constructs the NiceGUI elements directly and reads their props,
same tmp-path RECIPE_BOOK_PATH isolation as test_recipe_store.py.
"""

from __future__ import annotations

import collections
from unittest.mock import MagicMock

import pytest
from nicegui import context, ui

import nicegui_dashboard.recipe_store as recipe_store
from nicegui_dashboard.pages.dashboard_page import create_dashboard_page

# Expected step per recipe-editor number field, in the order they are
# created in dashboard_page.py (RO Water Volume ... pH adjustment factor).
# nutrient_b_percent_display (readonly, auto-calculated) is excluded - it
# has no user-facing +/- stepping.
EXPECTED_STEPS = [1, 0.1, 10, 1, 1, 0.01, 0.1, 1, 1, 1, 10, 0.01]


@pytest.fixture(autouse=True)
def _isolated_recipe_book(tmp_path, monkeypatch):
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    return tmp_path


def _fake_controller() -> MagicMock:
    controller = MagicMock()
    controller.get_sensor_status.return_value = collections.defaultdict(lambda: None)
    controller.get_process_status.return_value = collections.defaultdict(lambda: None)
    controller.get_process_control_status.return_value = collections.defaultdict(lambda: None)
    return controller


def test_recipe_editor_number_fields_have_field_appropriate_step():
    before_ids = set(context.client.elements)

    with ui.column():
        create_dashboard_page(_fake_controller())

    new_elements = [
        element
        for element_id, element in context.client.elements.items()
        if element_id not in before_ids
    ]
    number_fields = [
        element
        for element in new_elements
        if isinstance(element, ui.number) and not element._props.get("readonly")
    ]

    steps = [element._props.get("step") for element in number_fields]
    assert steps == EXPECTED_STEPS
