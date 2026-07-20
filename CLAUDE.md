# CDS-Projektregeln

- Keine Hardware aktivieren oder GPIO-Ausgänge schalten.
- Keine Firmware flashen.
- `hardware_execution_enabled` nicht aktivieren.
- Keine Commits und keinen Push durchführen.
- Bestehende Änderungen nicht zurücksetzen.
- Sicherheitsrelevante Unklarheiten nicht erraten.
- Fail-closed implementieren.
- Änderungen immer mit automatisierten hardwarefreien Tests absichern.
- In externen Dokumentationen keine Firmennamen verwenden.
- Nach Änderungen `pytest`, `git diff --check` und einen Abschlussbericht ausgeben.
- keine smileys.
- sudo systemctl restart cds-nicegui-dashboard.service darf durchgeführt werden.