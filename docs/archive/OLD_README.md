# Central Dosing System – Python Control Layer

Stand: 07.07.2026  
Projektstatus: Entwicklungs- und Validierungsphase  
Ziel: sichere, nachvollziehbare und modular erweiterbare Steuerungslogik für den wasserbasierten CDS-Prozess.

---

## 1. Kurzüberblick

Dieses Repository enthält die Python-Implementierung für die Steuerungs- und Visualisierungsebene eines Central-Dosing-Systems. Der aktuelle Fokus liegt auf dem sicheren wasserbasierten Grundprozess:

```text
RO-Wasser → Mixing Tank → Sensorbox-Zirkulation → Drain
```

Chemikaliendosierung, Peristaltikpumpensteuerung und produktive Rezeptausführung sind noch nicht aktiv. Diese Funktionen werden erst nach weiterer Hardware-, Sensor- und Sicherheitsvalidierung ergänzt.

Die Steuerungslogik wurde von einzelnen Testskripten in eine modulare Struktur überführt. `main.py` ist der zentrale Einstiegspunkt für Preflight, Water-Cycle, Dashboard, Kalibrierung und Statusprüfungen.

---

## 2. Aktueller Funktionsstand

Aktuell umgesetzt:

- MQTT-Sensor-Bridge für OPC-UA-Sensordaten
- NiceGUI-Dashboard zur Visualisierung
- modularer Water-Cycle-Prozess
- Refill-Phase für den Mixing Tank
- Sensorbox-Zirkulationsphase
- Drain-Phase
- zentraler Preflight-Check
- GPIO-Konfliktprüfung gegen Node-RED-GPIO-Helper
- Recipe-Editor im Dashboard mit drei Favoriten und JSON-Ablage
- Mixing-Tank-Kalibrierung mit abgesichertem Drain-Timeout
- zentrale Systemkonfiguration für OPC-UA, MQTT und Mixer-Level-Kalibrierung

Der neue modulare Water-Cycle startet softwareseitig korrekt. Ein vollständiger realer Hardwarelauf nach dem letzten Refactor steht noch aus.

---

## 3. Zentrale Einstiegspunkte

Alle Hauptfunktionen sollen über `main.py` gestartet werden.

```bash
python main.py --help
python main.py preflight
python main.py gpio-check
python main.py water-cycle
python main.py calibrate-mixer
python main.py dashboard
python main.py sensor-bridge-check
python main.py safe-drain
```

Wichtige Befehle:

```bash
cd ~/cds_control
source .venv/bin/activate
python main.py preflight
```

Vor jedem Hardwaretest muss der kombinierte Preflight erfolgreich sein.

---

## 4. Projektstruktur

```text
cds_control/
├── main.py
├── config/
│   ├── system_config.json
│   ├── water_cycle_settings.json
│   ├── calibration_settings.json
│   └── process_settings.json
├── process/
│   ├── common.py
│   ├── refill.py
│   ├── sensor_circulation.py
│   ├── drain.py
│   └── water_cycle.py
├── services/
│   ├── system_config.py
│   ├── mqtt_publisher.py
│   ├── process_run_logger.py
│   └── sensor_snapshot.py
├── hardware/
│   ├── digital_output.py
│   └── actuator_manager.py
├── nicegui_dashboard/
│   ├── app.py
│   ├── cds_controller.py
│   ├── process_controller.py
│   ├── recipe_store.py
│   ├── mqtt_topic_reader.py
│   ├── pages/
│   └── static/
├── scripts/
│   └── check_gpio_conflicts.py
├── recipes/
│   └── dashboard_recipes.json
├── logs/
├── calibration_data/
├── mqtt_sensor_bridge.py
├── calibration_mixing_tank.py
├── preflight_check.py
└── requirements.txt
```

---

## 5. Konfiguration

Zentrale technische Systemwerte liegen in:

```text
config/system_config.json
```

Dort sind definiert:

- OPC-UA Endpoint
- OPC-UA Read Timeout
- MQTT Host
- MQTT Port
- MQTT Topics
- MQTT QoS
- MQTT Publish Timeout
- Mixer-Level-Kalibrierung

Beispiel:

```json
{
  "opcua": {
    "endpoint": "opc.tcp://10.8.0.62:14840",
    "read_timeout_seconds": 3.0
  },
  "mqtt": {
    "host": "localhost",
    "port": 1883,
    "sensor_topic": "cds/status/sensors",
    "process_topic": "cds/status/process",
    "qos": 1,
    "publish_timeout_seconds": 2.0
  },
  "mixer_level_calibration": {
    "volume_liters": 200.0,
    "factor": 0.175,
    "offset": 0.0,
    "status": "temporary_factor_0_175"
  }
}
```

Prozessbezogene Water-Cycle-Werte liegen in:

```text
config/water_cycle_settings.json
```

Kalibrierbezogene Einstellungen liegen in:

```text
config/calibration_settings.json
```

---

## 6. Sicherheitsgrundsätze

Das System ist bewusst defensiv aufgebaut.

Wichtige Regeln:

- Hardwareausführung ist im Repository standardmäßig deaktiviert.
- `hardware_execution_enabled` muss für reale Tests bewusst lokal auf `true` gesetzt werden.
- Vor jedem Hardwarelauf muss `python main.py preflight` erfolgreich sein.
- Node-RED darf keine GPIOs blockieren, die Python für den CDS-Prozess benötigt.
- Chemikaliendosierung ist aktuell nicht aktiv.
- Produktive Peristaltikpumpensteuerung ist aktuell nicht aktiv.
- Sensorwerte werden nicht blind als zuverlässige Wahrheit angenommen.
- Aktoren werden über zentrale Safe-Shutdown-Pfade ausgeschaltet.
- Manuelle Kalibrier-Drain-Funktionen besitzen ein Safety-Timeout.
- KeyboardInterrupt wird im Water-Cycle einheitlich behandelt.

Standardzustand in den Configs:

```json
"hardware_execution_enabled": false
```

Dadurch werden keine GPIO-Ausgänge initialisiert oder geschaltet, solange die Hardwareausführung nicht explizit aktiviert wird.

---

## 7. Preflight und GPIO-Konfliktprüfung

Der kombinierte Preflight prüft:

- wichtige Projektdateien
- Python-Syntax relevanter Module
- GPIO-Konfiguration
- doppelte GPIO-Zuordnungen
- aktive Systemdienste
- MQTT-Erreichbarkeit
- OPC-UA-Lesbarkeit
- aktuelle Sensor-MQTT-Payload-Struktur
- Node-RED-GPIO-Konflikte

Start:

```bash
python main.py preflight
```

Zusätzlicher Einzeltest:

```bash
python main.py gpio-check
```

Kritische CDS-GPIOs für Python:

```text
GPIO20 = mixer_refill_pump
GPIO21 = valve_0_drain
GPIO22 = contactor_2 / sensor_circulation_pump
GPIO26 = transfer_pump
```

Node-RED darf diese Pins nicht blockieren. Falls Node-RED nur unabhängige Funktionen wie RO-Machine betreibt, ist das zulässig, solange keine CDS-GPIOs blockiert werden.

---

## 8. Sensor-Bridge

Die Datei `mqtt_sensor_bridge.py` liest Sensordaten über OPC-UA und veröffentlicht sie per MQTT.

Topic:

```text
cds/status/sensors
```

Veröffentlichte Werte:

- RO-Tank-Füllstand
- Mixing-Tank-Füllstand
- EC
- pH
- Wassertemperatur
- Dissolved Oxygen
- Bridge-Status
- Fehlerstatus

Der OPC-UA-Read ist mit Timeout abgesichert. MQTT wird mit QoS 1 und Publish-Bestätigung verwendet.

Servicebefehle:

```bash
systemctl status cds-sensor-bridge.service
sudo systemctl restart cds-sensor-bridge.service
journalctl -u cds-sensor-bridge.service -f
```

MQTT-Test:

```bash
mosquitto_sub -h localhost -t cds/status/sensors -v
```

---

## 9. Water-Cycle-Prozess

Der aktuelle Water-Cycle ist modular aufgebaut:

```text
process/water_cycle.py
→ Orchestrator

process/refill.py
→ Mixing Tank befüllen

process/sensor_circulation.py
→ Sensorbox durchströmen

process/drain.py
→ Mixing Tank entleeren

process/common.py
→ gemeinsame Hilfsfunktionen
```

Start:

```bash
python main.py water-cycle
```

Der Water-Cycle führt automatisch zuerst den Preflight aus. Wenn der Preflight fehlschlägt, startet der Prozess nicht.

Aktueller Ablauf:

```text
1. Sicherheitsabfrage
2. Sensor-Payload prüfen
3. Refill bis Zielwert
4. optionale Sensorbox-Zirkulation
5. optionale Drain-Phase
6. Safe-Shutdown
```

Wichtiger Status:

```text
Der modulare Water-Cycle muss nach dem Refactor noch einmal vollständig real mit Hardware getestet werden.
```

---

## 10. NiceGUI-Dashboard

Das NiceGUI-Dashboard visualisiert:

- RO-Tank
- Mixing Tank
- pH
- EC
- Temperatur
- Dissolved Oxygen
- Sensor-Bridge-Status
- Prozessstatus
- Aktorstatus
- Rezept-/Sollwerte
- Prozesslog

Start über systemd:

```bash
sudo systemctl restart cds-nicegui-dashboard.service
systemctl status cds-nicegui-dashboard.service --no-pager
```

Manueller Start:

```bash
python main.py dashboard
```

Die Oberfläche ist HMI/Visualisierung. Hardwarelogik soll weiterhin in Python-Prozessmodulen bleiben.

---

## 11. Recipe-Editor

Im Dashboard existiert ein Recipe-Editor mit drei Favoriten.

Ablage:

```text
recipes/dashboard_recipes.json
```

Aktueller Zweck:

- Rezeptwerte anzeigen
- Rezeptfavoriten bearbeiten
- JSON speichern
- spätere Prozess-/Dosierlogik vorbereiten

Noch nicht aktiv:

- automatische Chemikaliendosierung
- Peristaltikpumpensteuerung
- automatische pH-/EC-Regelung

Diese Funktionen werden erst später auf Basis der gespeicherten Rezeptwerte ergänzt.

---

## 12. Kalibrierung

Die Mixing-Tank-Kalibrierung erfolgt über:

```bash
python main.py calibrate-mixer
```

Die Kalibrierung liest OPC-UA-Rohwerte und speichert Messpunkte als CSV unter:

```text
calibration_data/
```

Die automatische Drain-Unterstützung in der Kalibrierung ist abgesichert durch:

```json
"calibration_drain_max_seconds": 120.0
```

Damit kann die Transferpumpe nicht unbegrenzt laufen, falls Enter vergessen wird oder die SSH-/Terminal-Session hängt.

---

## 13. Git- und Repo-Hygiene

Nicht ins Repository gehören:

```text
__pycache__/
*.pyc
.venv/
logs/*
*.log
*.out
*.bak_*
calibration_data/*.csv
archive/
```

Wichtige Regel:

```text
Git-History ersetzt lokale Backup-Dateien.
```

Vor Commits prüfen:

```bash
git status
git diff --check
python main.py preflight
```

---

## 14. Dependencies

Die wichtigsten Python-Abhängigkeiten stehen in:

```text
requirements.txt
```

Installation in einer virtuellen Umgebung:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 15. Aktuell offene Punkte

Nächste sinnvolle Schritte:

1. Modularen Water-Cycle real mit Hardware testen.
2. README und Sicherheitsstand aktuell halten.
3. Logging schrittweise von `print()` auf `logging` umstellen.
4. MQTT-Staleness-/Payload-Reader vereinheitlichen.
5. Automatisierte Tests für config- und safety-nahe Funktionen ergänzen.
6. Recipe-Editor-UX später weiter verbessern.
7. Rezeptwerte kontrolliert mit Water-Cycle-Settings verbinden.
8. Peristaltikpumpensteuerung erst nach weiterer Sicherheitsvalidierung vorbereiten.

---

## 16. Entwicklungsregel

Für dieses Projekt gilt:

```text
Erst sicher.
Dann nachvollziehbar.
Dann automatisiert.
Dann produktiv.
```

Neue Hardwarefunktionen werden nur aufgenommen, wenn:

- die reale Hardwarezuordnung geprüft wurde,
- der Wasserpfad nachvollziehbar sicher ist,
- Preflight erfolgreich ist,
- ein Safe-Shutdown vorhanden ist,
- ein Timeout oder plausibler Abbruchpfad existiert,
- und der Ablauf zunächst mit Wasser getestet wurde.