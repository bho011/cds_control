# CDS Control – Python/NiceGUI Steuerung

**Projektkontext:** Central Dosing System (CDS)  
**Stand:** 06.07.2026  
**Status:** Validierungs- und Entwicklungsstand, keine produktive Chemikaliendosierung

Diese README beschreibt den aktuellen kompakten Stand der Python-/NiceGUI-Implementierung für das CDS. Ziel ist eine gut wartbare, sichere und schrittweise erweiterbare Steuerungslogik für Wasserprozess, Sensorik, Dashboard und spätere Rezept-/Dosierfunktionen.

---

## 1. Kurzüberblick

Das CDS wird aktuell schrittweise von einzelnen Hardwaretests zu einer strukturierten Python-Steuerung überführt.

Aktueller Fokus:

- sichere Hardware-Ansteuerung über GPIO/Relais,
- OPC-UA-Sensoranbindung,
- MQTT-Statuskommunikation,
- NiceGUI-Dashboard,
- modularer Water-Cycle-Prozess,
- Recipe-Editor als Dashboard-Visualisierung,
- Sicherheitsprüfungen vor Hardwareläufen.

Aktuell **nicht aktiv**:

- produktive Chemikaliendosierung,
- automatische Peristaltikpumpensteuerung,
- automatische Rezeptausführung mit Dosierung,
- Routing zu Solution Tanks,
- produktiver Dauerbetrieb.

---

## 2. Aktueller Prozessstand

Der zentrale Wasserpfad wurde hardwareseitig erfolgreich getestet:

```text
RO-Tank → Refill-Pumpe → Mixing Tank
Mixing Tank → Sensorbox → zurück in Mixing Tank
Mixing Tank → Transferpumpe + Drain-Ventil → Drain
```

Validiert wurde:

- Befüllen des Mixing Tanks,
- Sensorbox-Zirkulation,
- Rückfluss aus der Sensorbox in den Mixing Tank,
- Drain über Transferpumpe und Drain-Ventil,
- Drain-Stopp anhand des Levelsignals bei leerem Tank.

Der neue modulare Einstieg über `python main.py water-cycle` startet und ist softwareseitig vorbereitet. Der **vollständige Hardwarelauf nach dem Refactor** muss jedoch noch einmal real am System getestet werden.

---

## 3. Wichtige Projektstruktur

```text
cds_control/
├── main.py
├── config/
│   ├── process_settings.json
│   ├── water_cycle_settings.json
│   └── refill_and_drain_test_settings.json
├── process/
│   ├── common.py
│   ├── refill.py
│   ├── sensor_circulation.py
│   ├── drain.py
│   └── water_cycle.py
├── hardware/
├── services/
├── nicegui_dashboard/
├── recipes/
│   └── dashboard_recipes.json
├── scripts/
│   └── check_gpio_conflicts.py
├── calibration_mixing_tank.py
├── mqtt_sensor_bridge.py
├── preflight_check.py
└── logs/
```

---

## 4. Hauptbefehle

Virtuelle Umgebung aktivieren:

```bash
cd ~/cds_control
source .venv/bin/activate
```

Hilfe anzeigen:

```bash
python main.py --help
```

Preflight ausführen:

```bash
python main.py preflight
```

GPIO-Konfliktcheck einzeln ausführen:

```bash
python main.py gpio-check
```

Sensor-Bridge-Status prüfen:

```bash
python main.py sensor-bridge-check
```

Water-Cycle starten:

```bash
python main.py water-cycle
```

Manuelles Safe-Drain-Tool starten:

```bash
python main.py safe-drain
```

---

## 5. Sicherheitsgrundsätze

Das Projekt ist bewusst defensiv aufgebaut. Hardware darf nicht unbeabsichtigt anlaufen.

### 5.1 Hardware ist im Repo standardmäßig deaktiviert

In den Konfigurationsdateien ist standardmäßig gesetzt:

```json
"hardware_execution_enabled": false
```

Betroffene Dateien:

```text
config/process_settings.json
config/water_cycle_settings.json
config/refill_and_drain_test_settings.json
```

Für echte Vor-Ort-Tests muss dieser Wert bewusst lokal auf `true` gesetzt werden. Zusätzlich ist der korrekte Bestätigungstext erforderlich.

### 5.2 Preflight vor Hardwareläufen

Vor jedem Hardwarelauf muss der kombinierte Preflight grün sein:

```bash
python main.py preflight
```

Der Preflight prüft unter anderem:

- wichtige Dateien,
- Python-Syntax,
- GPIO-Konfiguration,
- doppelte GPIO-Belegungen,
- MQTT-Verbindung,
- OPC-UA-Lesetest,
- Sensor-Payload-Struktur,
- Sensor-Bridge-Status,
- Node-RED-/GPIO-Konflikte.

Der Preflight ist hardwareseitig read-only und schaltet keine Pumpen, Ventile oder Relais.

### 5.3 Node-RED darf keine Python-CDS-GPIOs blockieren

Node-RED darf für getrennte Funktionen genutzt werden, darf aber keine kritischen GPIOs blockieren, die Python für den CDS-Prozess benötigt.

Kritisch für Python-CDS:

```text
GPIO20 = mixer_refill_pump
GPIO21 = valve_0_drain
GPIO22 = contactor_2 / sensor_circulation_pump
GPIO26 = transfer_pump
```

Der GPIO-Konfliktcheck meldet einen Fehler, wenn Node-RED diese Pins über `nrpio.py` belegt.

### 5.4 Emergency Stop und Thread-Sicherheit

Der NiceGUI-Controller wurde gehärtet:

- kein `daemon=True` für Prozess-Threads,
- kein Doppelstart während Cleanup/Teardown,
- Emergency Stop verhindert erneutes Einschalten durch parallele Update-Ticks,
- Aktoren werden per `safe_shutdown_all()` abgeschaltet,
- Hintergrundthread kann gejoint werden,
- `is_running` wird nicht mehr zu früh freigegeben.

### 5.5 Drain- und Kalibrier-Sicherheit

Die Kalibrierfunktion für den Mixing Tank steuert Drain-Ventil und Transferpumpe nicht mehr unbegrenzt bis Enter, sondern besitzt einen Safety-Timeout:

```json
"calibration_drain_max_seconds": 120.0
```

Enter stoppt weiterhin manuell früher, aber die Pumpe läuft nicht mehr unbegrenzt.

### 5.6 KeyboardInterrupt-Verhalten

`STRG+C` wurde im Water-Cycle vereinheitlicht:

- Phase wird kontrolliert beendet,
- zugehörige Aktoren werden ausgeschaltet,
- Folgephasen werden übersprungen,
- globaler Safe-Shutdown läuft trotzdem.

---

## 6. Sensorik und MQTT

### 6.1 Sensor-Bridge

Die Datei `mqtt_sensor_bridge.py` liest OPC-UA-Werte und veröffentlicht sie per MQTT.

Wichtiger Dienst:

```bash
systemctl status cds-sensor-bridge.service
journalctl -u cds-sensor-bridge.service -f
sudo systemctl restart cds-sensor-bridge.service
```

MQTT-Topic:

```text
cds/status/sensors
```

Prüfen:

```bash
mosquitto_sub -h localhost -t cds/status/sensors -C 1 -v
```

### 6.2 Aktuelle Sensorwerte

Aktuell werden verarbeitet:

- RO-Tank-Level,
- Mixing-Tank-Level,
- EC,
- pH,
- Temperatur,
- gelöster Sauerstoff / DO.

pH und DO wurden an der Hardware geprüft und sind nicht vertauscht.

### 6.3 Mixing-Tank-Level

Der Mixing-Tank-Levelsensor ist grundsätzlich nutzbar für:

- leer/voll-Erkennung,
- Trend,
- Prozessabschaltung,
- Drain-Erkennung.

Die Litergenauigkeit ist noch nicht final validiert. Es gibt bereits Kalibrierdaten, aber die endgültige Sensorformel oder Kalibriertabelle muss noch sauber abgeleitet und getestet werden.

---

## 7. Water-Cycle-Prozess

Der neue Water-Cycle ist modular aufgebaut:

```text
process/water_cycle.py          → Orchestrator
process/refill.py               → Befüllen bis absoluter Zielwert
process/sensor_circulation.py   → Sensorbox-Zirkulation
process/drain.py                → Drain mit Sensor-Leererkennung und Timeout
process/common.py               → gemeinsame Helfer, Logging, Sensorwertauswertung, Settings
```

Aktueller Ablauf:

```text
Preflight
→ Sicherheitsbestätigung
→ Refill bis Zielmenge
→ optionale Sensorzirkulation
→ optionale Drain-Abfrage
→ Drain bis leer oder Timeout
→ Safe-Shutdown
```

Wichtig: Der modulare `water-cycle` muss nach dem Refactor noch einmal mit echter Hardware vollständig getestet werden.

---

## 8. NiceGUI-Dashboard

Das NiceGUI-Dashboard ist die aktuelle Python-basierte Bedien- und Visualisierungsebene für das CDS.

Aktuelle Funktionen:

- Tank-/Füllstandsanzeige,
- Sensorwerte,
- Systemstatus,
- Prozessstatus,
- Prozesslog,
- Recipe-/Sollwertkarte,
- Recipe-Editor mit drei Favoriten.

Service:

```bash
systemctl status cds-nicegui-dashboard.service
sudo systemctl restart cds-nicegui-dashboard.service
```

Manueller Start:

```bash
python -m nicegui_dashboard.app
```

---

## 9. Recipe-Editor

Der Recipe-Editor ist im Dashboard integriert und speichert aktuell drei Favoriten in:

```text
recipes/dashboard_recipes.json
```

Aktuelle Rezeptfelder:

- Rezeptname,
- Ziel-Tank,
- Zielmenge Mixing Tank,
- EC-Sollwert,
- EC-Mischzeit,
- Stock-Volumen,
- pH-Sollwert,
- Säure-/Base-Volumen,
- pH-Mischzeit,
- Addons,
- Sensorzirkulation,
- Sensorpumpenzeit,
- Drain nach Prozess,
- Notiz.

Wichtig: Der Recipe-Editor ist aktuell **nur Visualisierung und JSON-Ablage**. Die Werte werden noch nicht automatisch für Peristaltikpumpen oder Dosierung verwendet.

---

## 10. Kalibrierung

Für den Mixing-Tank-Levelsensor gibt es:

```bash
python calibration_mixing_tank.py
```

Das Skript unterstützt:

- Nullpunkt,
- manuelle Fill-Messpunkte,
- Drain-Messpunkte,
- CSV-Logging,
- lineare Auswertung,
- Dokumentation der aktuellen Bridge-Kalibrierung.

Daten liegen unter:

```text
calibration_data/
```

Die Ergebnisse sollen später genutzt werden für:

- finale Sensorformel,
- Kalibriertabelle,
- Interpolation,
- Plausibilitätsgrenzen.

---

## 11. Git- und Datei-Hygiene

Nicht ins Repo gehören:

```text
__pycache__/
*.pyc
*.bak_*
.venv/
temporäre Logs
```

Wichtiger Statuscheck:

```bash
git status
```

Commit-Beispiel:

```bash
git add .
git commit -m "Describe change"
git push origin main
```

Falls zusätzlich ein GitLab-Remote genutzt wird:

```bash
git remote -v
git push gitlab main
```

---

## 12. Offene Punkte

Aktuell offen:

- modularen `python main.py water-cycle` einmal vollständig mit Hardware testen,
- Mixing-Tank-Levelsensor final kalibrieren,
- Recipe-Werte kontrolliert mit `water_cycle_settings.json` verbinden,
- Recipe-Editor-UX weiter verbessern,
- Peristaltikpumpensteuerung erst nach stabiler Recipe-/Prozessbasis planen,
- pH-/EC-Regelung erst nach Sensorvalidierung,
- Logik für spätere Dosier- und Mischprozesse definieren,
- alte Referenz-Testskripte erst nach erfolgreichem modularen Hardwaretest verschieben.

---

## 13. Aktuelle Entwicklungsregel

```text
Sicherheit vor Komfort.
Hardware nur nach grünem Preflight.
Kein Start ohne bewusste Hardwarefreigabe.
Keine Chemie, solange Wasserprozess und Sensorik nicht stabil validiert sind.
```

Der nächste empfohlene technische Schritt ist ein kontrollierter Vor-Ort-Test des modularen Water-Cycle-Prozesses mit `hardware_execution_enabled=true` in der lokalen Config und anschließendem Log-Review.