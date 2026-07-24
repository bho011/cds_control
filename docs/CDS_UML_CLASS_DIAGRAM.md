# CDS-Projekt: UML-Klassendiagramm

Diese Dateien wurden aus dem aktuellen Python-Projektstand abgeleitet:

- `CDS_UML_CLASS_DIAGRAM.puml`: PlantUML-Quelle fuer das UML-Klassendiagramm
- `CDS_UML_CLASS_DIAGRAM.md`: kurze Einordnung und Render-Hinweise

## Fokus des Diagramms

Das Diagramm zeigt die produktiven Kernklassen aus:

- `domain`: Rezeptmodell, Vorschau, Run-Optionen und unveraenderlicher RunConfig-Snapshot
- `nicegui_dashboard`: Dashboard-Orchestrierung und Prozesssteuerung
- `hardware`: GPIO-Ausgaenge und zentraler ActuatorManager mit Hardware-Lock
- `process`: Hintergrundprozesse fuer Manual Drain, Tank Cleaning und Peristaltik-Prime
- `statemachine`: Fill-and-Measure-Zustandsmaschine
- `services`: MQTT, Sensor-Snapshots, Settings-Validierung und Prozesslogging
- `services.peristaltic`: Mapping, Firmwareprofile, Kalibrierung, Prime, serieller MCU-Client

Tests, Cache-Dateien, Logs, virtuelle Umgebung und reine CLI-Hilfsskripte sind bewusst nicht als eigene Hauptpakete aufgenommen, damit das Diagramm lesbar bleibt.

## Architektur kurz

`CdsController` ist der Einstiegspunkt des NiceGUI-Dashboards. Er besitzt einen `SensorSnapshotReader`, einen `MqttTopicReader` und den zentralen `ProcessController`.

`ProcessController` verhindert konkurrierende Hardwarelaeufe und koordiniert:

- `FillAndMeasureStateMachine`
- `ManualDrainJog`
- `TankCleaningController`
- `PumpPrimeController`

`ManualDrainJog`, `TankCleaningController` und `PumpPrimeController` erben von `BackgroundHardwareProcess`. Die GPIO-Hardware wird erst beim Start eines Worker-Threads ueber `ActuatorManager` initialisiert.

Die Rezeptlogik ist bewusst getrennt: Ein editierbares `StoredRecipe` wird validiert und beim Prozessstart in einen unveraenderlichen `RunConfigSnapshot` ueberfuehrt.

Die Peristaltik-Schicht ist eigenstaendig: `PumpPrimeController` nutzt Mapping, Firmwareprofile, Kalibrierdaten, `PeristalticSerialClient` und `PeristalticSessionLogger`.

## Rendern

Falls PlantUML installiert ist:

```bash
plantuml -tsvg docs/CDS_UML_CLASS_DIAGRAM.puml
plantuml -tpng docs/CDS_UML_CLASS_DIAGRAM.puml
```

Viele IDEs koennen die `.puml`-Datei auch direkt anzeigen, z.B. mit einer PlantUML-Erweiterung.
