from nicegui import app as nicegui_app
from nicegui import ui

from nicegui_dashboard.cds_controller import CdsController
from nicegui_dashboard.pages.dashboard_page import create_dashboard_page


controller = CdsController()
nicegui_app.on_shutdown(controller.close)


@ui.page("/")
def index() -> None:
    create_dashboard_page(controller)


print("[INFO] Starting CDS NiceGUI Dashboard on 0.0.0.0:8081")

ui.run(
    title="CDS NiceGUI Dashboard",
    host="0.0.0.0",
    port=8081,
    reload=False,
)
