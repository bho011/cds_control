# Central Dosing System – Python Control Layer

Stand: 17.07.2026
Projektstatus: Entwicklungs- und Validierungsphase
Ziel: sichere, nachvollziehbare und modular erweiterbare Steuerungslogik für den wasserbasierten CDS-Prozess.

> **Sicherheitsstatus in einem Satz:** Hardwareausführung ist im Repository standardmäßig deaktiviert (`hardware_execution_enabled: false`); von den 15 konfigurierten GPIO-Ausgängen sind aktuell nur **drei** real am physischen System validiert (siehe Abschnitt 4). Alle anderen Ausgänge sind softwareseitig ansteuerbar, aber ohne bestätigte reale Wirkung.

---

## 1. Kurzüberblick

Dieses Repository enthält die Python-Implementierung für die Steuerungs- und Visualisierungsebene eines Central-Dosing-Systems (Raspberry Pi, GPIO-Relais, OPC-UA-Sensorik, MQTT/Node-RED). Der aktuelle Fokus liegt auf dem sicheren wasserbasierten Grundprozess:

```text
RO-Wasser → Mixing Tank → Sensorbox-Zirkulation → Drain
```

Chemikaliendosierung und Peristaltikpumpensteuerung sind noch nicht aktiv. Diese Funktionen werden erst nach weiterer Hardware-, Sensor- und Sicherheitsvalidierung ergänzt. Die Rezeptausführung ist bisher nur für den reinen Wasser-Zielwert (Füllvolumen, Sensorzirkulation) wirksam mit dem tatsächlichen Prozessstart verbunden (siehe Abschnitt 15) — die EC-/pH-/Dosierwerte im Rezept sind weiterhin rein deskriptiv, ohne Konsument im Code.

Die Steuerungslogik wurde von einzelnen Testskripten in eine modulare Struktur überführt. `main.py` ist der zentrale Einstiegspunkt für Preflight, Water-Cycle, Dashboard, Kalibrierung und Statusprüfungen.

**Zielplattform:** Raspberry Pi (Raspberry Pi OS), Python 3.11, `.venv`-basiertes Setup. Die GPIO-Ansteuerung läuft über `gpiozero` mit dem `lgpio`-Pin-Factory-Backend.

---

## 2. Architektur / Datenfluss

```text
                     ┌──────────────────────┐
                     │   OPC-UA-Server      │
                     │ (RO/Mixing-Tank,     │
                     │  pH, EC, Temp, DO)   │
                     └──────────┬───────────┘
                                │ OPC-UA read (Timeout-abgesichert)
                                ▼
                     mqtt_sensor_bridge.py  ──► MQTT: cds/status/sensors
                                                        │
gpio_config.py ──► hardware/ ──► process/ ──► statemachine/            │
   (Pin-Mapping)   (DigitalOutput,  (refill/drain/   (FillAndMeasure)  │
                    ActuatorManager) sensor_circulation/               │
                                     auto_circulation/                 │
                                     manual_drain_jog/                 │
                                     tank_cleaning)                    │
        │                 │                │                           │
        └─────────────────┴────────────────┴──► MQTT: cds/status/process
                                                        │
                                          ┌─────────────┴──────────────┐
                                          ▼                            ▼
                                  Node-RED Dashboard         NiceGUI Dashboard
                                  (bestehende HMI)           (nicegui_dashboard/)
```

`main.py` ist der einzige unterstützte Einstiegspunkt für alle produktiven Abläufe (Preflight, Water-Cycle, Dashboard, Kalibrierung, Safe-Drain, Statuschecks). Manual Drain Jog und Tank Cleaning werden ausschließlich über das NiceGUI-Dashboard gestartet, nicht über `main.py`.

---

## 3. Aktueller Funktionsstand

Aktuell umgesetzt:

- MQTT-Sensor-Bridge für OPC-UA-Sensordaten (Read-Timeout + QoS-1-Publish mit Bestätigung)
- NiceGUI-Dashboard zur Visualisierung und Prozesssteuerung (überarbeitetes 4-Spalten-Layout, siehe Abschnitt 14)
- modularer Water-Cycle-Prozess (Refill → Sensorbox-Zirkulation → Drain)
- automatische Zirkulationssteuerung (`process/auto_circulation.py`) – füllstandsabhängiges Ein-/Ausschalten der Zirkulationspumpen, wiederverwendet von Water-Cycle **und** Tank Cleaning
- Manual Drain Jog – dashboardgesteuerte manuelle Entleerung mit Server-seitigem 30-Sekunden-Watchdog
- Tank Cleaning – automatischer Reinigungszyklus (Fill → Hold → Drain), siehe Abschnitt 13
- zentraler Preflight-Check inkl. GPIO-Konfliktprüfung gegen Node-RED-GPIO-Helper, per CLI **und** per "Run Preflight Check"-Button im Dashboard (Dev Info)
- Recipe-Editor im Dashboard mit drei Favoriten und JSON-Ablage
- Mixing-Tank-Kalibrierung mit pumpengesteuerten Fill-/Drain-Segmenten, sekündlichem Trace-Log und offline nachrechenbarer Formel (`--analyze`), siehe Abschnitt 16
- zentrale Systemkonfiguration für OPC-UA, MQTT und Mixer-Level-Kalibrierung (`config/system_config.json`), inzwischen auch vom Dashboard-MQTT-Layer und der State-Machine-Kalibrierung genutzt (siehe Abschnitt 7)
- zentrale, race-freie Prozessverriegelung für Start/Stop von Fill-and-Measure, Manual Drain Jog und Tank Cleaning, plus prozessübergreifende Hardware-Sperre (`fcntl.flock`, `hardware/actuator_manager.py`) und nicht-blockierender, asynchroner Emergency Stop (siehe Abschnitt 8)
- einheitliche Watchdog-/Fortschrittsprüfung für alle Fill-/Drain-Phasen (`process/watchdog.py`) und Settings-Schema-Validierung mit gesammelten Fehlern statt Abbruch beim ersten Treffer (`services/settings_validation.py`), beide mit `pytest`-Testabdeckung (siehe Abschnitt 9)
- Recipe-Editor tatsächlich mit dem Prozessstart verbunden: Zielvolumen und Sensorzirkulation aus dem aktiven Rezept bestimmen jetzt real den nächsten Fill-and-Measure-Lauf statt nur die Anzeige (siehe Abschnitt 15)

Der modulare Water-Cycle startet softwareseitig korrekt. Ein vollständiger realer Hardwarelauf nach dem letzten Refactor steht noch aus. Manual Drain Jog und Tank Cleaning sind ebenfalls noch nicht real mit Hardware validiert (siehe Abschnitt 17).

---

## 4. Hardware-Validierungsstatus (wichtig!)

Nur die folgenden Ausgänge wurden real am physischen System getestet und ihre Wirkung bestätigt:

| Funktion              | Config-Key             | GPIO (BCM) | Physischer Pin | Status              |
|------------------------|------------------------|:----------:|:---------------:|----------------------|
| Mixer Refill Pump       | `mixer_refill_pump`    | 20         | 38               | validiert            |
| Supply/Test Valve 6     | `test_supply_valve_6`  | 6          | 31               | validiert            |
| Drain Valve 0           | `valve_0_drain`        | 21         | 40               | validiert            |

Alle übrigen Einträge in `gpio_config.py` (`contactor_0/5`, `transfer_pump`, `mixing_circulation_pump`, `sensor_circulation_pump`, `valve_1`–`valve_9`) sind im Code ansteuerbar, aber **nicht** durch einen realen Hardwaretest bestätigt. Insbesondere:

- **Transfer Pump** (`transfer_pump`, GPIO26) wird bereits in `calibration_mixing_tank.py`, `main_safe_drain.py` und jetzt auch in `process/tank_cleaning.py` verwendet, ihre elektrische Anbindung ist aber laut Projektstand noch nicht abschließend geklärt.
- **Valve 5** ist laut Kommentar in `gpio_config.py` nicht zuverlässig dicht.
- **Tank Cleaning** steuert zusätzlich `mixing_circulation_pump` und `sensor_circulation_pump` an – ebenfalls noch ohne realen Hardwaretest. Die Settings-Datei (`config/tank_cleaning_settings.json`) ist deshalb bewusst auf ein reduziertes **40-Liter-Testvolumen** statt des späteren 200-Liter-Zielwerts gestellt, um den ersten Hardwaretest ohne größeren Wasserverbrauch/-risiko durchzuführen.

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

Manual Drain Jog und Tank Cleaning haben keinen eigenen `main.py`-Befehl – sie werden ausschließlich über die entsprechenden Buttons im NiceGUI-Dashboard gestartet (`python main.py dashboard`).

---

## 6. Projektstruktur

```text
cds_control/
├── main.py
├── config/
│   ├── system_config.json
│   ├── water_cycle_settings.json
│   ├── calibration_settings.json
│   ├── process_settings.json
│   └── tank_cleaning_settings.json
├── process/
│   ├── common.py
│   ├── refill.py
│   ├── sensor_circulation.py
│   ├── drain.py
│   ├── water_cycle.py
│   ├── auto_circulation.py
│   ├── manual_drain_jog.py
│   ├── tank_cleaning.py
│   ├── watchdog.py
│   └── background_process.py
├── services/
│   ├── system_config.py
│   ├── mqtt_publisher.py
│   ├── process_run_logger.py
│   ├── sensor_snapshot.py
│   └── settings_validation.py
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
├── tests/
├── logs/
├── calibration_data/
├── mqtt_sensor_bridge.py
├── calibration_mixing_tank.py
├── main_safe_drain.py
├── preflight_check.py
├── requirements.txt
├── requirements-dev.txt
└── pytest.ini
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
    "factor": 0.610566,
    "offset": -26.093067,
    "status": "fitted_ge10L_20260703_20260709_sessions",
    "fit_r2": 0.996399,
    "fit_max_abs_error_liters": 5.698,
    "fit_source": "calibration_mixing_tank.py --analyze ... (siehe Abschnitt 16)"
  }
}
```

> Hinweis: Diese Datei ist inzwischen die alleinige Quelle für OPC-UA-Endpoint, MQTT-Verbindung und Mixer-Kalibrierung — sowohl `mqtt_sensor_bridge.py`, `calibration_mixing_tank.py`, `preflight_check.py` als auch der Dashboard-MQTT-Layer (`services/mqtt_publisher.py`, `nicegui_dashboard/mqtt_topic_reader.py`, `nicegui_dashboard/cds_controller.py`) und der Kalibrierfaktor-Fallback in `statemachine/fill_and_measure_state_machine.py` lesen jetzt aus `get_mqtt_config()`/`get_mixer_level_calibration()` statt aus eigenen hartcodierten Werten.

Weitere Settings-Dateien, je Prozess eine eigene:

- `config/water_cycle_settings.json` – Water-Cycle
- `config/calibration_settings.json` – Mixing-Tank-Kalibrierung
- `config/process_settings.json` – Fill-and-Measure
- `config/tank_cleaning_settings.json` – Tank Cleaning (Zielvolumen, Haltezeit, Auto-Zirkulations-Schwellen, Drain-Parameter, eigener `hardware_execution_enabled`-Schalter)

---

## 8. Sicherheitsgrundsätze

- Hardwareausführung ist im Repository standardmäßig deaktiviert (`hardware_execution_enabled: false`).
- Muss für reale Tests bewusst lokal auf `true` gesetzt werden — nie mit `true` committen.
- Jede Prozess-Settings-Datei (Water-Cycle, Fill-and-Measure, Kalibrierung, Tank Cleaning) hat ihren **eigenen** `hardware_execution_enabled`-Schalter und Bestätigungstext.
- Vor jedem Hardwarelauf muss `python main.py preflight` erfolgreich sein.
- Node-RED darf keine GPIOs blockieren, die Python für den CDS-Prozess benötigt.
- Chemikaliendosierung und Peristaltikpumpensteuerung sind aktuell nicht aktiv.
- Sensorwerte werden nicht blind als zuverlässige Wahrheit angenommen (Filterung, Plausibilitätsprüfung, Confirm-Samples).
- Aktoren werden über zentrale Safe-Shutdown-Pfade ausgeschaltet (`ActuatorManager.safe_shutdown_all()`), Fehler dabei werden geloggt statt verschluckt.
- Manuelle Kalibrier-Drain-Funktionen besitzen ein Safety-Timeout (`calibration_drain_max_seconds`).
- `KeyboardInterrupt` wird in allen Water-Cycle-Phasen einheitlich behandelt und bricht den Rest des Ablaufs kontrolliert ab.
- Manual Drain Jog und Tank Cleaning laufen als Hintergrund-Threads, deren Schleifen alle ~0,5s auf ein `threading.Event` prüfen — ein Emergency Stop aus dem Dashboard unterbricht sie dadurch binnen einer knappen Sekunde, statt bis zum Ende der laufenden Phase zu warten.
- Start/Stop von Fill-and-Measure, Manual Drain Jog und Tank Cleaning sind atomar gegen den internen `ProcessController`-Lock abgesichert: zwei gleichzeitige Start-Aufrufe (Dashboard-Klick + parallel gestartetes Skript) können nicht mehr beide durchkommen — vormals ein TOCTOU-Race zwischen Zustandsprüfung und tatsächlichem Start.
- Zusätzlich verhindert eine prozessübergreifende Datei-Sperre (`fcntl.flock`, `hardware/actuator_manager.py`, `logs/.hardware.lock`) parallele Hardware-Ansteuerung durch zwei unabhängige Python-*Prozesse* (nicht nur Threads im selben Prozess) — z. B. Dashboard plus ein direkt gestartetes Kalibrier- oder Safe-Drain-Skript.
- Emergency Stop ist asynchron implementiert (`ProcessController.emergency_stop()`): das Signalisieren (State-Machine-Abbruch, `request_stop()`, Aktor-Shutdown) passiert sofort unter Lock, das Warten auf den Thread-Join läuft danach außerhalb des Locks in einem Worker-Thread — das Dashboard bleibt für andere verbundene Clients währenddessen reaktionsfähig, statt bis zu ~7s einzufrieren.

Standardzustand in den Configs:

```json
"hardware_execution_enabled": false
```

Dadurch werden keine GPIO-Ausgänge initialisiert oder geschaltet, solange die Hardwareausführung nicht explizit aktiviert wird.

---

## 9. Preflight und GPIO-Konfliktprüfung

Der kombinierte Preflight prüft: Projektdateien, Python-Syntax, GPIO-Konfiguration, doppelte GPIO-Zuordnungen (**blockiert den Water-Cycle-Start**, kein reines Warnen mehr), aktive Systemdienste, MQTT-Erreichbarkeit, OPC-UA-Lesbarkeit, aktuelle Sensor-MQTT-Payload-Struktur, Node-RED-GPIO-Konflikte.

```bash
python main.py preflight
python main.py gpio-check
```

Zusätzlich per Dashboard auslösbar: Button "Run Preflight Check" in Dev Info schreibt das komplette Ergebnis ins Process Log (läuft in einem Worker-Thread, blockiert das Dashboard für andere Nutzer währenddessen nicht).

Kritische CDS-GPIOs für Python:

```text
GPIO20 = mixer_refill_pump
GPIO21 = valve_0_drain
GPIO22 = sensor_circulation_pump
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

### Automatisierte Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Deckt Locking (`nicegui_dashboard/process_controller.py`, prozessübergreifende
Sperre in `hardware/actuator_manager.py`), Watchdog-/Fortschrittsprüfungen
(`process/watchdog.py`) und Settings-Validierung (`services/settings_validation.py`,
inklusive aller vier echten `config/*.json`-Dateien) ab — kein GPIO/OPC-UA
wird dabei angefasst. `requirements-dev.txt` (nur `pytest` zusätzlich zu
`requirements.txt`) ist bewusst getrennt von der Produktions-`requirements.txt`.

---

## 10. Sensor-Bridge

`mqtt_sensor_bridge.py` liest Sensordaten über OPC-UA und veröffentlicht sie per MQTT auf `cds/status/sensors` (RO-/Mixing-Tank-Füllstand, EC, pH, Wassertemperatur, Dissolved Oxygen, Bridge-/Fehlerstatus). OPC-UA-Reads sind mit Timeout abgesichert, MQTT-Publish läuft mit QoS 1 und Bestätigung.

Die Bridge erkennt jetzt auch eine mittendrin gestorbene OPC-UA-Session (mehrere Zyklen in Folge, in denen alle Sensoren gleichzeitig fehlschlagen) und verbindet sich automatisch neu, mit Backoff (5s, verdoppelnd bis 30s). Vorher blieb eine tote Session unbemerkt bestehen — der Prozess lief dann beliebig lange weiter und veröffentlichte nur noch Fehler/`null`-Werte, ohne sich je selbst zu erholen.

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
process/auto_circulation.py    → füllstandsabhängige Zirkulationspumpen-Steuerung
process/common.py              → gemeinsame Hilfsfunktionen
```

```bash
python main.py water-cycle
```

Der Water-Cycle führt automatisch zuerst den Preflight aus; schlägt dieser fehl, startet der Prozess nicht.

```text
1. Sicherheitsabfrage
2. Sensor-Payload prüfen
3. Refill bis Zielwert (Zirkulationspumpen schalten automatisch ab auto_circulation_start_liters zu)
4. optionale Sensorbox-Zirkulation
5. optionale Drain-Phase (Zirkulationspumpen schalten automatisch unter auto_circulation_stop_liters ab)
6. Safe-Shutdown
```

> Der modulare Water-Cycle muss nach dem letzten Refactor noch einmal vollständig real mit Hardware getestet werden.

---

## 12. Manual Drain Jog

Dashboard-Wartungsfunktion für kontrolliertes manuelles Entleeren, unabhängig vom Water-Cycle.

```text
process/manual_drain_jog.py → ManualDrainJog
```

Ablauf:

- "Hold to Drain"-Button im Dashboard: Drain-Ventil öffnet, kurze Ventil-Settle-Zeit, dann Transferpumpe an.
- Solange der Button gehalten wird, läuft die Pumpe.
- Loslassen stoppt sofort (dead-man-Prinzip).
- Server-seitiger Watchdog stoppt zusätzlich spätestens nach 30 Sekunden, auch falls die Verbindung zum Browser/Websocket hängt.

Läuft als eigener Hintergrund-Thread mit `threading.Event`-Steuerung, damit ein Emergency Stop jederzeit zuverlässig durchgreift. Wechselseitig ausgeschlossen mit Fill-and-Measure und Tank Cleaning – es kann immer nur einer der drei Prozesse gleichzeitig aktiv sein.

---

## 13. Tank Cleaning

Automatisierter Reinigungszyklus für den Mixing Tank, z. B. nach einem Mischvorgang.

```text
process/tank_cleaning.py       → TankCleaningController
config/tank_cleaning_settings.json → Einstellungen
```

Ablauf (drei Phasen, alle über MQTT auf `cds/status/process` sichtbar):

```text
1. FILL    Mixer Refill Pump füllt bis zum Zielvolumen (target_fill_total_liters)
2. HOLD    feste Haltezeit (cleaning_hold_seconds, Default 300 s)
3. DRAIN   Transfer Pump + Drain Valve entleeren bis Sensor "leer" bestätigt
```

Während FILL, HOLD und DRAIN läuft dieselbe `AutoCirculationController`-Logik wie im Water-Cycle mit: Zirkulationspumpen (`mixing_circulation_pump`, `sensor_circulation_pump`) schalten automatisch ein, sobald der Tank `auto_circulation_start_liters` (Default 30 L) überschreitet, und wieder aus unter `auto_circulation_stop_liters` (Default 25 L) – auch während des Drains. Die 300-Sekunden-Haltezeit selbst beginnt erst, sobald das Zielvolumen sensorbestätigt erreicht ist, nicht schon beim Einschalten der Pumpen bei 30 L.

Sicherheit:

- Eigener `hardware_execution_enabled`-Schalter + Bestätigungstext in `config/tank_cleaning_settings.json`, unabhängig von den anderen Prozessen.
- Jede Phasen-Schleife prüft alle 0,5 s ein `threading.Event` – Stop-Button oder Emergency Stop wirken dadurch nahezu sofort, nicht erst am Ende der laufenden Phase.
- Fill-Phase mit denselben Sicherheitsprüfungen wie `process/refill.py` (Timeout, Fortschrittskontrolle, max. Tankfüllstand, RO-Mindestmenge).
- Drain-Phase mit demselben berechneten Timeout wie `process/drain.py`.
- `safe_shutdown` schaltet auf jedem Ausstiegspfad (Erfolg, Abbruch, Fehler) alle beteiligten Aktoren ab.

**Aktueller Testzustand:** `config/tank_cleaning_settings.json` ist auf ein reduziertes **Testvolumen von 40 L** gestellt (statt der späteren 200 L), damit der erste reale Hardwaretest ohne großen Wasserverbrauch durchgeführt werden kann. `hardware_execution_enabled` steht auf `false` und muss vor einem echten Lauf bewusst und nur nach physischer Prüfung des Wasserwegs auf `true` gesetzt werden.

Dashboard: Button "Start Tank Cleaning" im Bereich "Process Control → Tank Cleaning", nutzt dasselbe Bestätigungsfeld wie "Start Process". Wechselseitig ausgeschlossen mit Fill-and-Measure und Manual Drain Jog.

---

## 14. NiceGUI-Dashboard

Das Dashboard wurde auf ein 4-Spalten-Layout umgebaut, das ohne Scrollen auskommt:

```text
┌─────────────┬───────────────────┬──────────────────────┬────────────────┐
│ System      │ Current Process   │ Process Control      │ Sensor Values  │
│ Status      │ State             │  - Safety            │                │
│             │                   │    Confirmation      │ Tanks / Levels │
│ Actuators / │ Recipe /          │  - Maintenance       │ (RO + Mixing,  │
│ Outputs     │ Setpoints         │    (Manual Drain Jog)│  untereinander │
│             │                   │  - Tank Cleaning     │  gestapelt)    │
└─────────────┴───────────────────┴──────────────────────┴────────────────┘
```

- **System Status / Actuators**: nur noch die tatsächlich genutzten Ausgänge (Mixer Refill Pump, Supply Valve 6, Drain Valve 0, Transfer Pump, Mixing/Sensor Circulation Pump, Valve 1–5). Unbenutzte Pins (`contactor_0/5`, `valve_7/8/9`) sind aus der Anzeige entfernt, um die Karte nicht unnötig zu füllen.
- **Process Control**: Safety Confirmation (ein gemeinsames Bestätigungsfeld für Start Process **und** Tank Cleaning) → Emergency Stop → Maintenance (Manual Drain Jog) → Tank Cleaning.
- **Dev Info**: Die früher permanent sichtbaren Status-Badges (RASPI LIVE / PYTHON CORE / Update) und das Prozess-Log sind hinter einem "Dev Info"-Button im Kopfbereich versteckt und öffnen sich als Dialog – für den Endanwender unnötige Diagnoseinfos stören dadurch nicht mehr die Hauptansicht.
- **Sensor Values / Tanks**: eigene rechte Spalte, RO- und Mixing-Tank-Gauge stehen untereinander (nicht mehr nebeneinander) und haben eine stabile Breite unabhängig von der Anzahl der Nachkommastellen des Füllstands.

```bash
sudo systemctl restart cds-nicegui-dashboard.service
systemctl status cds-nicegui-dashboard.service --no-pager
python main.py dashboard   # manueller Start
```

Die Oberfläche ist HMI/Visualisierung plus Start/Stop-Steuerung für Fill-and-Measure, Manual Drain Jog und Tank Cleaning. Die eigentliche Prozesslogik bleibt in den Python-Prozessmodulen (`process/`, `statemachine/`), nicht im UI-Code.

---

## 15. Recipe-Editor

Recipe-Editor mit drei Favoriten unter `recipes/dashboard_recipes.json`. Aktueller Zweck: Rezeptwerte anzeigen/bearbeiten, JSON speichern, spätere Prozess-/Dosierlogik vorbereiten.

**Tatsächlich wirksam** (`nicegui_dashboard/recipe_store.py::build_run_config()`, aufgerufen von `ProcessController.start_fill_and_measure()`): `target_fill_total_liters` bestimmt das reale Füll-Zielvolumen (erzwingt `fill_mode="absolute"`), `sensor_circulation_enabled` bestimmt, ob die Sensorbox-Zirkulationspumpe beim Lauf mit angesteuert wird. Beide Werte werden zusätzlich gegen `max_mixer_liters` und das `process_settings.json`-Schema geprüft — ein Rezept-Zielvolumen über der Mixer-Kapazität blockiert den Start mit klarer Meldung, statt den Prozess zu starten und erst per Watchdog mittendrin abzubrechen.

Noch nicht aktiv (kein Konsument im Code): die übrigen 17 Rezeptfelder — automatische Chemikaliendosierung, Peristaltikpumpensteuerung, automatische pH-/EC-Regelung.

---

## 16. Kalibrierung

```bash
python main.py calibrate-mixer
```

Liest OPC-UA-Rohwerte, speichert Messpunkte als CSV unter `calibration_data/`.

**Ablauf einer Live-Session:**

1. **0–30 L manuell**: Wasser per Eimer/Kanne zuführen, Menge eingeben, gemittelter Messpunkt (Settle-Zeit + N Samples) wie gehabt.
2. **30–200 L pumpengesteuert**: Die Mixer-Refill-Pumpe füllt automatisch, einmal 20 L (Rundung auf die erste Tankmarkierung), danach in 25-L-Schritten. Jedes Segment loggt den Rohwert **sekündlich** in eine separate `..._trace.csv`. Enter stoppt die Pumpe, sobald die Markierung erreicht ist; ein Safety-Timeout pro Segment (`calibration_fill_max_seconds`, Default 300s) schützt die Pumpe, falls das mal vergessen wird.
3. **200–0 L pumpengesteuert**: Transferpumpe + Drainventil, gleiches Prinzip in 25-L-Schritten runter bis 0 L, ebenfalls mit Trace-Log und eigenem Segment-Timeout (`calibration_drain_max_seconds`, Default 180s).

**Sicherheit:** Die früheren getippten Bestätigungsphrasen (`required_confirmation_text` etc.) und die `hardware_execution_enabled`-Abfrage wurden auf ausdrücklichen Wunsch aus diesem Skript entfernt — es wird ohnehin nur manuell, mit anwesendem Bediener am Tank, gestartet. Der reine Pumpenschutz (Segment-Timeout) blieb bewusst bestehen.

**Offline nachrechnen, ohne Hardware:**

```bash
python calibration_mixing_tank.py --analyze calibration_data/*.csv
```

Lädt beliebig viele bereits gespeicherte CSVs, poolt sie, normalisiert Nullpunkte pro Session, warnt bei nicht-monotonen Messpunkten, und rechnet mehrere Fit-Varianten (u. a. mit einem `min_reference_volume_l`-Filter, da der Sensor unter ~10 L bekannt unzuverlässig ist). Rührt garantiert kein GPIO/OPC-UA an — reine CSV-Auswertung.

**Aktuelle Formel** (`config/system_config.json` → `mixer_level_calibration`):

```text
Liter_real = 0.610566 * sensor_raw + -26.093067
R² = 0.996, max. Fehler ≈ 5.7 L, Bereich 10-175 L
```

Abgeleitet aus allen bisherigen Kalibrierungssessions (zero-normalisiert, nur Fill-Phase, ≥10 L). Ein einzelner Ausreißer-Messpunkt (erster Pump-Checkpoint der letzten Session, vermutlich ungenau abgelesene Markierung) wurde dabei bewusst als `valid_for_fit=False` markiert, siehe `invalid_reason` in der jeweiligen CSV. **175–200 L bleibt unbestätigt** — dort gibt es keine verlässliche Tankmarkierung zum Gegenchecken.

---

## 17. Aktuell offene Punkte

1. **Tank Cleaning auf Testvolumen** — `config/tank_cleaning_settings.json` ist aktuell auf 40 L (Testlauf) statt der geplanten 200 L gestellt; nach erfolgreichem erstem Hardwaretest zurück auf das Zielvolumen setzen.
2. **Tank Cleaning und Manual Drain Jog real mit Hardware validieren** — beide Funktionen sind bisher nur softwareseitig getestet, `hardware_execution_enabled` steht in beiden zugehörigen Settings-Dateien auf `false`.
3. **Kalibrierbereich 175–200 L absichern** — bisher keine verlässliche Tankmarkierung am oberen Ende zum Gegenchecken; die produktive Formel extrapoliert dort leicht über den bestätigten Bereich hinaus.
4. Modularen Water-Cycle real mit Hardware testen.
5. Logging schrittweise von `print()` auf `logging` umstellen (bisher nur `actuator_manager.py`, `cds_controller.py`).
6. MQTT-Staleness-/Payload-Reader (`services/sensor_snapshot.py`, `nicegui_dashboard/mqtt_topic_reader.py`) vereinheitlichen.
7. Kein CI, kein Linting/mypy trotz durchgängiger Typannotationen (`tests/` mit `pytest` für Locking/Watchdogs/Config-Validierung existiert inzwischen, siehe Abschnitt 9).
8. ~~Rezeptwerte kontrolliert mit Water-Cycle-Settings verbinden.~~ Erledigt: `nicegui_dashboard/recipe_store.py::build_run_config()` verbindet die zwei Rezeptfelder mit einem echten Konsumenten (`target_fill_total_liters` → `fill_mode="absolute"`/`target_total_liters`, `sensor_circulation_enabled` → `enable_sensor_circulation`) beim Start von Fill-and-Measure, inklusive `max_mixer_liters`-Grenzprüfung. Die übrigen 17 Rezeptfelder (EC/pH/Dosierung) bleiben ohne Konsument, solange Chemikaliendosierung nicht implementiert ist.
9. **Peristaltikpumpensteuerung** — Firmware-seitige Härtung ist im separaten Repository `central_dosing_sys_peristaltic` (Branch `harden-serial-protocol`) abgeschlossen und auf beiden Arduino-MCUs real verifiziert: Treiber standardmäßig deaktiviert, Dosis-/Laufzeitlimits (`MAX_SINGLE_DOSE_ML`, `MAX_RUNTIME_MS`), maschinenlesbares `PING`/`STATUS`/`DOSE`/`STOP`/`STOPALL`-Protokoll (siehe dortige `docs/SERIAL_PROTOCOL.md`). In `cds_control` selbst existiert noch **kein** Python-Serial-Client dafür — die Anbindung an Rezeptlogik und NiceGUI-Dashboard ist bewusst ein separater, noch nicht begonnener Schritt und setzt weitere Sicherheitsvalidierung (reale Kalibrierung, verifizierte Pumpen-Zuordnung) voraus.

---

## 18. Git- und Repo-Hygiene

Nicht ins Repository sind:

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

## 19. Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 20. Entwicklungsregel

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
