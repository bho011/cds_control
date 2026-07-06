# Central Dosing System – Python Control Layer

Stand: 07.07.2026
Projektstatus: Entwicklungs- und Validierungsphase
Ziel: sichere, nachvollziehbare und modular erweiterbare Steuerungslogik für den wasserbasierten CDS-Prozess.

> **Sicherheitsstatus in einem Satz:** Hardwareausführung ist im Repository standardmäßig deaktiviert (`hardware_execution_enabled: false`); von den 15 konfigurierten GPIO-Ausgängen sind aktuell nur **drei** real am physischen System validiert (siehe Abschnitt 4). Alle anderen Ausgänge sind softwareseitig ansteuerbar, aber ohne bestätigte reale Wirkung.

---

## 1. Kurzüberblick

Dieses Repository enthält die Python-Implementierung für die Steuerungs- und Visualisierungsebene eines Central-Dosing-Systems (Raspberry Pi, GPIO-Relais, OPC-UA-Sensorik, MQTT/Node-RED). Der aktuelle Fokus liegt auf dem sicheren wasserbasierten Grundprozess:

```text
RO-Wasser → Mixing Tank → Sensorbox-Zirkulation → Drain
```

Chemikaliendosierung, Peristaltikpumpensteuerung und produktive Rezeptausführung sind noch nicht aktiv. Diese Funktionen werden erst nach weiterer Hardware-, Sensor- und Sicherheitsvalidierung ergänzt.

Die Steuerungslogik wurde von einzelnen Testskripten in eine modulare Struktur überführt. `main.py` ist der zentrale Einstiegspunkt für Preflight, Water-Cycle, Dashboard, Kalibrierung und Statusprüfungen.

**Zielplattform:** Raspberry Pi (Raspberry Pi OS), Python 3.11, `.venv`-basiertes Setup. Die GPIO-Ansteuerung läuft über `gpiozero` mit dem `lgpio`-Pin-Factory-Backend.

---

## 2. Architektur / Datenfluss

```text
                     ┌──────────────────────┐
                     │   OPC-UA-Server       │
                     │ (RO/Mixing-Tank,      │
                     │  pH, EC, Temp, DO)    │
                     └──────────┬───────────┘
                                │ OPC-UA read (Timeout-abgesichert)
                                ▼
                     mqtt_sensor_bridge.py  ──► MQTT: cds/status/sensors
                                                        │
gpio_config.py ──► hardware/ ──► process/ ──► statemachine/            │
   (Pin-Mapping)   (DigitalOutput,  (refill/drain/   (FillAndMeasure)  │
                    ActuatorManager) sensor_circulation)               │
        │                 │                │                          │
        └─────────────────┴────────────────┴──► MQTT: cds/status/process
                                                        │
                                          ┌─────────────┴──────────────┐
                                          ▼                            ▼
                                  Node-RED Dashboard         NiceGUI Dashboard
                                  (bestehende HMI)           (nicegui_dashboard/)
```

`main.py` ist der einzige unterstützte Einstiegspunkt für alle produktiven Abläufe (Preflight, Water-Cycle, Dashboard, Kalibrierung, Safe-Drain, Statuschecks).

---

## 3. Aktueller Funktionsstand

Aktuell umgesetzt:

- MQTT-Sensor-Bridge für OPC-UA-Sensordaten (Read-Timeout + QoS-1-Publish mit Bestätigung)
- NiceGUI-Dashboard zur Visualisierung
- modularer Water-Cycle-Prozess (Refill → Sensorbox-Zirkulation → Drain)
- zentraler Preflight-Check inkl. GPIO-Konfliktprüfung gegen Node-RED
- Recipe-Editor im Dashboard mit drei Favoriten und JSON-Ablage
- Mixing-Tank-Kalibrierung mit abgesichertem Drain-Timeout
- zentrale Systemkonfiguration für OPC-UA, MQTT und Mixer-Level-Kalibrierung (`config/system_config.json`)

Der modulare Water-Cycle startet softwareseitig korrekt. Ein vollständiger realer Hardwarelauf nach dem letzten Refactor steht noch aus.

---

## 4. Hardware-Validierungsstatus (wichtig!)

Nur die folgenden Ausgänge wurden real am physischen System getestet und ihre Wirkung bestätigt:

| Funktion              | Config-Key             | GPIO (BCM) | Physischer Pin | Status              |
|------------------------|------------------------|:----------:|:---------------:|----------------------|
| Mixer Refill Pump       | `mixer_refill_pump`    | 20         | 38               | validiert            |
| Supply/Test Valve 6     | `test_supply_valve_6`  | 6          | 31               | validiert            |
| Drain Valve 0           | `valve_0_drain`        | 21         | 40               | validiert            |

Alle übrigen Einträge in `gpio_config.py` (`contactor_0/2/3/5`, `transfer_pump`, `valve_1`–`valve_9`) sind im Code ansteuerbar, aber **nicht** durch einen realen Hardwaretest bestätigt. Insbesondere:

- **Transfer Pump** (`transfer_pump`, GPIO26) wird bereits in `calibration_mixing_tank.py` und `main_safe_drain.py` verwendet, ihre elektrische Anbindung ist aber laut Projektstand noch nicht abschließend geklärt.
- **Valve 5** ist laut Kommentar in `gpio_config.py` nicht zuverlässig dicht.

Bevor ein neuer Ausgang produktiv verwendet wird: real am System schalten, Wirkung visuell/physisch bestätigen, erst danach in eine automatisierte Sequenz aufnehmen.

---

## 5. Zentrale Einstiegspunkte

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

```bash
cd ~/cds_control
source .venv/bin/activate
python main.py preflight
```

Vor jedem Hardwaretest muss der kombinierte Preflight erfolgreich sein.

---

## 6. Projektstruktur

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
├── main_safe_drain.py
├── preflight_check.py
└── requirements.txt
```

---

## 7. Konfiguration

Zentrale technische Systemwerte liegen in `config/system_config.json`:

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

> Hinweis: Aktuell lesen `mqtt_sensor_bridge.py`, `calibration_mixing_tank.py` und `preflight_check.py` aus dieser Datei. Der Dashboard-MQTT-Layer (`services/mqtt_publisher.py`, `nicegui_dashboard/mqtt_topic_reader.py`, `nicegui_dashboard/cds_controller.py`) und der Kalibrierfaktor-Fallback in `statemachine/fill_and_measure_state_machine.py` nutzen sie noch nicht — siehe Abschnitt 15.

Prozessbezogene Water-Cycle-Werte: `config/water_cycle_settings.json`
Kalibrierbezogene Einstellungen: `config/calibration_settings.json`

---

## 8. Sicherheitsgrundsätze

- Hardwareausführung ist im Repository standardmäßig deaktiviert (`hardware_execution_enabled: false`).
- Muss für reale Tests bewusst lokal auf `true` gesetzt werden — nie mit `true` committen.
- Vor jedem Hardwarelauf muss `python main.py preflight` erfolgreich sein.
- Node-RED darf keine GPIOs blockieren, die Python für den CDS-Prozess benötigt.
- Chemikaliendosierung und Peristaltikpumpensteuerung sind aktuell nicht aktiv.
- Sensorwerte werden nicht blind als zuverlässige Wahrheit angenommen (Filterung, Plausibilitätsprüfung, Confirm-Samples).
- Aktoren werden über zentrale Safe-Shutdown-Pfade ausgeschaltet (`ActuatorManager.safe_shutdown_all()`), Fehler dabei werden geloggt statt verschluckt.
- Manuelle Kalibrier-Drain-Funktionen besitzen ein Safety-Timeout (`calibration_drain_max_seconds`).
- `KeyboardInterrupt` wird in allen Water-Cycle-Phasen einheitlich behandelt und bricht den Rest des Ablaufs kontrolliert ab.

---

## 9. Preflight und GPIO-Konfliktprüfung

Der kombinierte Preflight prüft: Projektdateien, Python-Syntax, GPIO-Konfiguration, doppelte GPIO-Zuordnungen (aktuell nur als **Warnung**, kein Abbruch), aktive Systemdienste, MQTT-Erreichbarkeit, OPC-UA-Lesbarkeit, aktuelle Sensor-MQTT-Payload-Struktur, Node-RED-GPIO-Konflikte.

```bash
python main.py preflight
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

### Troubleshooting

| Preflight-Fehler | Wahrscheinliche Ursache | Nächster Schritt |
|---|---|---|
| MQTT nicht erreichbar | Broker nicht gestartet | `systemctl status mosquitto` |
| OPC-UA nicht lesbar | Server nicht erreichbar / falscher Endpoint | `config/system_config.json` prüfen, Netzwerk prüfen |
| GPIO-Konflikt mit Node-RED | Node-RED-Flow nutzt denselben Pin | Node-RED-Flow-Zuordnung prüfen, `scripts/check_gpio_conflicts.py` |
| Sensor-Payload ungültig | `cds-sensor-bridge.service` läuft nicht/hängt | `journalctl -u cds-sensor-bridge.service -f` |

---

## 10. Sensor-Bridge

`mqtt_sensor_bridge.py` liest Sensordaten über OPC-UA und veröffentlicht sie per MQTT auf `cds/status/sensors` (RO-/Mixing-Tank-Füllstand, EC, pH, Wassertemperatur, Dissolved Oxygen, Bridge-/Fehlerstatus). OPC-UA-Reads sind mit Timeout abgesichert, MQTT-Publish läuft mit QoS 1 und Bestätigung.

```bash
systemctl status cds-sensor-bridge.service
sudo systemctl restart cds-sensor-bridge.service
journalctl -u cds-sensor-bridge.service -f
mosquitto_sub -h localhost -t cds/status/sensors -v
```

---

## 11. Water-Cycle-Prozess

```text
process/water_cycle.py         → Orchestrator
process/refill.py              → Mixing Tank befüllen
process/sensor_circulation.py  → Sensorbox durchströmen
process/drain.py               → Mixing Tank entleeren
process/common.py              → gemeinsame Hilfsfunktionen
```

```bash
python main.py water-cycle
```

Der Water-Cycle führt automatisch zuerst den Preflight aus; schlägt dieser fehl, startet der Prozess nicht.

```text
1. Sicherheitsabfrage
2. Sensor-Payload prüfen
3. Refill bis Zielwert
4. optionale Sensorbox-Zirkulation
5. optionale Drain-Phase
6. Safe-Shutdown
```

> Der modulare Water-Cycle muss nach dem letzten Refactor noch einmal vollständig real mit Hardware getestet werden.

---

## 12. NiceGUI-Dashboard

Visualisiert RO-Tank, Mixing Tank, pH, EC, Temperatur, Dissolved Oxygen, Sensor-Bridge-Status, Prozessstatus, Aktorstatus, Rezept-/Sollwerte, Prozesslog.

```bash
sudo systemctl restart cds-nicegui-dashboard.service
systemctl status cds-nicegui-dashboard.service --no-pager
python main.py dashboard   # manueller Start
```

Die Oberfläche ist HMI/Visualisierung. Hardwarelogik soll weiterhin in Python-Prozessmodulen bleiben.

---

## 13. Recipe-Editor

Recipe-Editor mit drei Favoriten unter `recipes/dashboard_recipes.json`. Aktueller Zweck: Rezeptwerte anzeigen/bearbeiten, JSON speichern, spätere Prozess-/Dosierlogik vorbereiten.

Noch nicht aktiv: automatische Chemikaliendosierung, Peristaltikpumpensteuerung, automatische pH-/EC-Regelung.

---

## 14. Kalibrierung

```bash
python main.py calibrate-mixer
```

Liest OPC-UA-Rohwerte, speichert Messpunkte als CSV unter `calibration_data/`. Die automatische Drain-Unterstützung ist per `calibration_drain_max_seconds` (Default 120s) gegen unbegrenztes Pumpenlaufen abgesichert.

---

## 15. Aktuell offene Punkte

1. **`requirements.txt` vervollständigen** — `gpiozero` fehlt, obwohl `hardware/digital_output.py` es direkt importiert; aktuell nur `lgpio` (Pin-Factory-Backend) gelistet.
2. **Getrackte Backup-Datei entfernen**: `process/water_cycle.py.bak_20260706_211431` ist trotz `.gitignore`-Regel weiterhin in Git (`git rm --cached ...`).
3. **Mixer-Kalibrierfaktor-Fallback in der State Machine zentralisieren** — `statemachine/fill_and_measure_state_machine.py` nutzt den importierten `get_mixer_level_calibration()` noch nicht, Fallback bleibt hart auf `1.0`.
4. **MQTT-Host/Port im Dashboard-Layer zentralisieren** — `services/mqtt_publisher.py`, `nicegui_dashboard/mqtt_topic_reader.py`, `nicegui_dashboard/cds_controller.py` sollen `services/system_config.get_mqtt_config()` verwenden statt eigener Defaults.
5. **GPIO-Duplikatsprüfung im Preflight verschärfen** — aktuell nur Warnung, sollte bei echten Duplikaten einen Fail auslösen.
6. Modularen Water-Cycle real mit Hardware testen.
7. Logging schrittweise von `print()` auf `logging` umstellen (bisher nur `actuator_manager.py`, `cds_controller.py`).
8. MQTT-Staleness-/Payload-Reader (`services/sensor_snapshot.py`, `nicegui_dashboard/mqtt_topic_reader.py`) vereinheitlichen.
9. Automatisierte Tests für config- und safety-nahe Funktionen ergänzen (aktuell kein `tests/`, kein CI, kein Linting/mypy trotz durchgängiger Typannotationen).
10. Rezeptwerte kontrolliert mit Water-Cycle-Settings verbinden.
11. Peristaltikpumpensteuerung erst nach weiterer Sicherheitsvalidierung vorbereiten.

---

## 16. Git- und Repo-Hygiene

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

## 17. Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Siehe Abschnitt 15, Punkt 1 zu einer aktuell fehlenden Dependency.

---

## 18. Entwicklungsregel

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
