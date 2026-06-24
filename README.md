
# CDS-System – Python-Implementierung

Stand: 22.06.2026  
Projektkontext: Central Dosing System (CDS)
Dokumentationszweck: Übersicht für GitHub / VSCode über den aktuellen Python-Umsetzungsstand

---

## 1. Ziel der Python-Umsetzung

Ziel der Python-Umsetzung ist es, zentrale Funktionen des CDS-Systems strukturiert, nachvollziehbar und testbar abzubilden. Dabei steht aktuell nicht die vollständige produktive Dosierung im Vordergrund, sondern die sichere technische Grundlage für spätere Automatisierungsschritte.

Die bisherige Python-Arbeit konzentriert sich auf folgende Bereiche:

- sicheres Ansteuern validierter Hardware-Ausgänge
- Aufbau einer ersten Prozesslogik als Zustandsmaschine
- Einbindung von Sensordaten über OPC UA
- Umrechnung relevanter Tankfüllstände
- Übertragung von Prozess- und Sensordaten per MQTT an Node-RED
- Vorbereitung einer späteren Integration in Dashboard, InfluxDB und Rezeptsteuerung

Codebeispiele sind in dieser Dokumentation bewusst nicht enthalten. Die Datei beschreibt den technischen Stand, die Architektur und die bisher validierten Ergebnisse.

---

## 2. Ausgangssituation

Das CDS-System besteht aus mehreren technischen Teilbereichen:

- Raspberry Pi als zentrale Steuerungs- und Integrationsplattform
- Node-RED als bestehende Bedien- und Visualisierungsebene
- OPC-UA-Schnittstelle für Sensordaten
- Relais-/GPIO-Ausgänge für Pumpen und Ventile
- RO-/Osmose-Wasserzuführung zum Mixing Tank
- Mixing Tank mit Sensorik für Prozesswerte
- Drain-Ausgang zum Entleeren beziehungsweise Ableiten von Wasser

Ein Teil der bestehenden Node-RED-Flows soll nach Möglichkeit unverändert bleiben, weil bestimmte Bereiche bereits vorhanden sind und vermutlich funktionieren. Die Python-Implementierung wurde daher ergänzend aufgebaut und greift zunächst nur auf validierte, sichere Testausgänge zu.

---

## 3. Grundprinzip der Umsetzung

Die Python-Umsetzung folgt einem vorsichtigen, schrittweisen Vorgehen:

1. Hardware-Ausgänge werden nicht blind verwendet.
2. Jeder relevante Ausgang wird zuerst einzeln validiert.
3. Erst danach wird der Ausgang in eine automatisierte Sequenz aufgenommen.
4. Automatisierte Abläufe werden zunächst nur mit Wasser getestet.
5. Chemikalien- oder Dosierfunktionen werden aktuell nicht automatisiert ausgeführt.
6. Statusinformationen werden über MQTT an Node-RED weitergegeben.
7. Sensorwerte werden separat gelesen, geprüft und erst danach für Steuerlogik verwendet.

Dadurch entsteht eine nachvollziehbare Trennung zwischen Hardware-Validierung, Prozesslogik, Sensorik und Visualisierung.

---

## 4. Umgesetzte Python-Bestandteile

### 4.1 GPIO- und Ausgangssteuerung

Es wurde eine Python-seitige Steuerung für validierte GPIO-/Relais-Ausgänge aufgebaut. Die Steuerung dient dazu, bestimmte Pumpen und Ventile gezielt ein- und auszuschalten.

Bisher sicher validierte beziehungsweise verwendete Ausgänge:

- Mixer Refill Pump über Contactor 1
- Valve 6 als getesteter Wasser-/Zulauf-Ausgang
- Drain-Ausgang über die aktuell vorgesehene Drain-Ventilzuordnung

Während der Tests wurde besonders darauf geachtet, nur Ausgänge zu verwenden, deren reale Wirkung physisch nachvollzogen wurde. Dadurch wurde verhindert, dass unklare oder falsch zugeordnete Relais unkontrolliert geschaltet werden.

### 4.2 Sichere Wasser-Testsequenz

Auf Basis der validierten Ausgänge wurde eine erste sichere Wasser-Testsequenz umgesetzt. Diese Sequenz bildet vereinfacht den Ablauf ab:

- Ventil öffnen
- Pumpe starten
- Wasser für eine definierte Zeit fördern
- Pumpe stoppen
- Ablauf beziehungsweise Drain öffnen
- System wieder in einen sicheren Zustand bringen

Der Test wurde physisch am CDS-System ausgeführt und erfolgreich validiert. Die Python-Steuerung konnte die ausgewählten Ausgänge zuverlässig ansteuern.

Eine wichtige Beobachtung aus dem Test war die Anlaufverzögerung der Mixer Refill Pump. Die Pumpe benötigt ungefähr zwei Sekunden, bis tatsächlich Wasser fließt. Diese Verzögerung muss bei späteren Laufzeit- oder Mengenberechnungen berücksichtigt werden.

### 4.3 Zustandsmaschine für den Wasserprozess

Für den kontrollierten Ablauf wurde eine erste Python-Zustandsmaschine umgesetzt. Die Zustandsmaschine bildet den Prozess nicht als lose Befehlsfolge ab, sondern als definierte Abfolge von Prozesszuständen.

Bisher verwendete Prozesszustände sind unter anderem:

- Öffnen der benötigten Ventile
- Starten der Pumpe
- laufender Förderprozess
- Stoppen der Pumpe
- Zurücksetzen beziehungsweise Beenden des Testablaufs

Der Vorteil dieser Struktur ist, dass der Prozess später einfacher erweitert werden kann. Zusätzliche Sicherheitsprüfungen, Sensorbedingungen, Zeitlimits, Rezeptparameter oder Fehlerzustände können gezielt in einzelne Zustände integriert werden.

### 4.4 MQTT-Statusübertragung an Node-RED

Die Python-Zustandsmaschine wurde so erweitert, dass sie Prozessinformationen per MQTT veröffentlicht. Node-RED kann diese Informationen empfangen und im Dashboard anzeigen.

Übertragen werden unter anderem:

- aktueller Prozesszustand
- Zeitstempel beziehungsweise Aktualisierungsstatus
- Status einzelner Ausgänge
- Fehler- oder Prozesshinweise, sofern vorhanden

Dadurch entsteht eine Verbindung zwischen der Python-Prozesslogik und der bestehenden Node-RED-Oberfläche. Node-RED muss den Prozess nicht selbst vollständig steuern, sondern kann den Python-Prozess überwachen und visualisieren.

### 4.5 MQTT-Sensorstatus

Neben dem Prozessstatus wurde auch die Übertragung von Sensordaten beziehungsweise berechneten Sensorwerten vorbereitet beziehungsweise umgesetzt.

Dabei werden relevante Werte an ein separates MQTT-Statusobjekt gesendet, sodass Node-RED diese Daten unabhängig vom Prozesszustand darstellen kann.

Der Sensorstatus ist damit logisch getrennt vom Prozessstatus:

- Prozessstatus beschreibt, was die Steuerung gerade macht.
- Sensorstatus beschreibt, welche Messwerte aktuell gelesen oder berechnet wurden.

Diese Trennung ist wichtig, damit Dashboard, spätere Rezeptlogik und mögliche Fehlerdiagnosen sauber voneinander abgegrenzt bleiben.

---

## 5. OPC-UA-Sensoranbindung

### 5.1 Verbindung zum OPC-UA-System

Es wurde ein Python-Test zur OPC-UA-Anbindung erstellt. Dieser konnte erfolgreich eine Verbindung zum internen OPC-UA-System herstellen und Sensorwerte lesen.

Besonders wichtig war dabei die Validierung des RO-Tank-Füllstands. Der gelesene Wert passte plausibel zum realen Tankstand. Damit wurde bestätigt, dass der verwendete OPC-UA-Wert dem RO-Tank zugeordnet werden kann.

### 5.2 RO-Tank-Berechnung

Für den RO-Tank wurde die Literberechnung in Python angepasst. Entscheidend war die Erkenntnis, dass der RO-Tank nicht mit 1000 Litern, sondern mit 1300 Litern Gesamtvolumen gerechnet werden muss.

Nach der Anpassung auf 1300 Liter waren die Python-Berechnung und die Node-RED-Dashboardanzeige deutlich besser synchronisiert.

Damit ist der RO-Tank-Wert aktuell einer der zuverlässigeren Sensorwerte im System.

### 5.3 Mixing-Tank-Füllstand

Der Mixing-Tank-Füllstand wurde ebenfalls betrachtet, ist aktuell aber noch nicht vollständig verlässlich nutzbar.

Bei einem Test außerhalb der finalen Einbausituation schwankte der berechnete Wert stark und zeigte unrealistische Literwerte. Das deutet darauf hin, dass die aktuell berechneten Mixing-Tank-Liter stark von Einbaulage, Rohwert, Druckverhältnissen und Tankgeometrie abhängen.

Für eine spätere Mengensteuerung im Mixing Tank ist daher noch eine saubere Kalibrierung notwendig.

---

## 6. Node-RED-Integration

Die Python-Implementierung wurde so aufgebaut, dass sie mit dem bestehenden Node-RED-System zusammenarbeiten kann.

Bisher umgesetzt beziehungsweise vorbereitet:

- Node-RED empfängt MQTT-Prozessstatus aus Python.
- Prozesszustände können im Dashboard angezeigt werden.
- Sensorwerte können über ein eigenes MQTT-Statusobjekt dargestellt werden.
- Der MQTT-Bridge-Status wurde klarer benannt, damit er nicht mit dem eigentlichen CDS-Prozesszustand verwechselt wird.
- Die Visualisierung trennt Prozessstatus, Sensorstatus und Systemstatus voneinander.

Damit ist Node-RED aktuell eher als HMI- und Dashboard-Schicht vorgesehen, während Python zunehmend die eigentliche Ablauf- und Steuerlogik übernimmt.

---

## 7. Sicherheitslogik und Testphilosophie

Die bisherige Python-Umsetzung wurde bewusst defensiv aufgebaut.

Wichtige Sicherheitsprinzipien:

- Es werden nur validierte Ausgänge geschaltet.
- Tests erfolgen zunächst nur mit Wasser.
- Keine automatische Chemikaliendosierung im aktuellen Stand.
- Unklare Hardware-Zuordnungen werden nicht produktiv verwendet.
- Der Prozess soll bei Fehlern oder Abbruch in einen sicheren Zustand zurückkehren.
- Pumpen und Ventile sollen nicht dauerhaft unkontrolliert aktiv bleiben.
- Sensorwerte werden erst nach Plausibilitätsprüfung für echte Steuerentscheidungen verwendet.

Dieses Vorgehen ist besonders wichtig, weil das reale System hydraulische, elektrische und prozesstechnische Abhängigkeiten besitzt.

---

## 8. Bisherige Testergebnisse

### 8.1 Hardwaretests

Die validierten Ausgänge konnten mit Python erfolgreich geschaltet werden. Die reale Wirkung der Ausgänge wurde am System nachvollzogen.

Ergebnis:

- Python kann die ausgewählten Relais-/GPIO-Ausgänge ansteuern.
- Die sichere Wasser-Testumgebung funktioniert.
- Der Ablauf kann kontrolliert gestartet und beendet werden.

### 8.2 Zustandsmaschinen-Test

Die erste Python-Zustandsmaschine wurde erfolgreich am realen Testaufbau ausgeführt.

Ergebnis:

- Die Prozessschritte laufen in der vorgesehenen Reihenfolge ab.
- Die Pumpe wird kontrolliert gestartet und gestoppt.
- Die Ventile werden passend zum Ablauf geschaltet.
- Statusmeldungen können während des Ablaufs übertragen werden.

### 8.3 OPC-UA-Test

Der OPC-UA-Test konnte Sensorwerte lesen und den RO-Tank plausibel auswerten.

Ergebnis:

- Verbindung zum OPC-UA-System erfolgreich.
- RO-Tank-Füllstand plausibel gelesen.
- RO-Literberechnung auf 1300 Liter angepasst.
- Python- und Dashboardwert sind dadurch besser synchronisiert.

### 8.4 MQTT-Test

Die MQTT-Veröffentlichung aus Python wurde erfolgreich mit Node-RED verbunden.

Ergebnis:

- Prozesszustände werden von Python veröffentlicht.
- Node-RED empfängt die Zustände.
- Dashboard-Elemente können darauf reagieren.
- Die Trennung zwischen Prozessstatus und Sensorstatus ist vorbereitet.

---

## 9. Aktueller technischer Stand

Aktuell existiert eine funktionierende Python-Grundlage für das CDS-System.

Der Stand umfasst:

- validierte GPIO-/Relais-Ansteuerung
- erste sichere Wasser-Testsequenz
- erste Python-Zustandsmaschine
- MQTT-Ausgabe des Prozessstatus
- MQTT-Ausgabe beziehungsweise Vorbereitung des Sensorstatus
- OPC-UA-Sensorlesetest
- RO-Tank-Literberechnung mit 1300 Litern
- Grundintegration mit Node-RED-Dashboard

Damit ist die technische Basis gelegt, um die Steuerungslogik schrittweise von einfachen Zeitabläufen zu einer sensorgestützten und später rezeptbasierten Prozesssteuerung auszubauen.

---

## 10. Noch offene Punkte

Folgende Punkte sind noch nicht vollständig abgeschlossen oder müssen später weiter validiert werden:

### 10.1 Mixing-Tank-Kalibrierung

Der Mixing-Tank-Füllstand muss sauber kalibriert werden. Dazu müssen Rohwert, Einbaulage, Tankgeometrie und reale Füllmengen miteinander abgeglichen werden.

Erst danach sollte der Mixing-Tank-Füllstand für automatische Abschaltbedingungen verwendet werden.

### 10.2 Rezeptbasierte Mengensteuerung

Die spätere Ziel-Funktion ist, dass ein Rezept eine gewünschte Wassermenge vorgibt und Python die Pumpe beziehungsweise Ventile automatisch stoppt, sobald die Zielmenge erreicht ist.

Dafür müssen folgende Grundlagen noch stabil sein:

- zuverlässiger Mixing-Tank-Füllstand
- definierte Startmenge
- definierte Zielmenge
- Fehlerbehandlung bei Sensorproblemen
- Timeout-Logik
- sichere Abschaltlogik

### 10.3 InfluxDB-Anbindung

Die Speicherung von Prozess- und Sensordaten in InfluxDB wurde bewusst zurückgestellt. Sie soll später ergänzt werden, wenn die Datenstruktur stabiler ist.

Sinnvoll wäre eine spätere Speicherung von:

- Sensorwerten
- Prozesszuständen
- Pumpen- und Ventilzuständen
- Fehlerzuständen
- Rezept- und Chargeninformationen

### 10.4 Weitere Hardware-Validierung

Einige Hardwarebereiche sind noch nicht vollständig validiert oder müssen vor produktiver Nutzung nochmals geprüft werden.

Dazu gehören insbesondere:

- Transfer Pump für das Entleeren des Mixing Tanks
- eventuell vorhandenes zusätzliches Ventil unterhalb des Mixing Tanks
- schwer zugängliche Komponenten im Hygienebereich
- nicht vollständig geklärte elektrische Versorgung einzelner Schütze oder Ausgänge
- Ventile mit möglichem Leckageverhalten

### 10.5 Fehler- und Sicherheitszustände

Die Zustandsmaschine sollte später um definierte Fehlerzustände erweitert werden.

Mögliche Fehlerfälle:

- Sensorwert fehlt
- Sensorwert ist unplausibel
- Zielmenge wird nicht erreicht
- Timeout beim Befüllen
- Pumpe läuft, aber Füllstand ändert sich nicht
- Ventil soll geschlossen sein, aber es findet weiterhin Durchfluss statt
- MQTT-Verbindung ist unterbrochen
- OPC-UA-Verbindung ist unterbrochen

---

## 11. Empfohlene nächste Entwicklungsschritte

Aus technischer Sicht sind die nächsten sinnvollen Schritte:

1. Python-Dateien und Konfiguration sauber strukturieren.
2. Ausgangs-Mapping zentral und eindeutig dokumentieren.
3. State Machine um Fehlerzustände erweitern.
4. Sensorwerte robuster lesen und validieren.
5. Mixing-Tank-Füllstand kalibrieren.
6. Rezeptparameter vorbereiten.
7. Zielmengen-Abschaltung auf Basis des Füllstands umsetzen.
8. MQTT-Dashboard weiter ausbauen.
9. InfluxDB erst integrieren, wenn die Datenpunkte stabil definiert sind.
10. Abschließende Testprotokolle für Hardware, Sensorik und Prozesslogik erstellen.

---

## 12. Abgrenzung

Diese Dokumentation beschreibt ausschließlich den aktuellen Python-Stand und die bisherige technische Umsetzung.

Nicht Bestandteil dieser Dokumentation sind:

- vollständige Node-RED-Flow-Dokumentation
- produktive Chemikaliendosierung
- vollständige elektrische Schaltpläne
- finale hydraulische Installation
- vollständige InfluxDB-Struktur
- produktive Rezeptverwaltung
- IHK-Projektdokumentation

---

## 13. Zusammenfassung

Im CDS-System wurde mit Python eine funktionsfähige technische Grundlage geschaffen. Die wichtigsten Ergebnisse sind die sichere Ansteuerung validierter Ausgänge, eine erste lauffähige Zustandsmaschine, die erfolgreiche OPC-UA-Sensoranbindung, die korrigierte RO-Tank-Literberechnung und die MQTT-Anbindung an Node-RED.

Damit ist Python aktuell als zentrale Steuerungs- und Integrationsschicht vorbereitet. Node-RED kann weiterhin für Visualisierung und Bedienung genutzt werden, während die eigentliche Prozesslogik schrittweise in Python aufgebaut wird.

Der nächste große Entwicklungsschritt ist die Umstellung von zeitbasierten Testabläufen auf eine sensorgestützte Mengensteuerung mit sauberer Fehlerbehandlung.

# Test zur steuerung der Valves und Contactoren war erfolgreich! Habe die Hardware mit Python unter kontrolle. 
# Pumpe und Ventile sind wie unten beschrieben angeschlossen so, dass RoWatter sicher nur in den Abfluss gepumpt wird bei Tests. 
# Alles andere ist genau so wie übernommen

Funktion              GPIO   Pin   Status
Mixer Refill Pump     20     38    valid / Python OK
Supply/Test Valve 6   6      31    valid / Python OK
Drain Valve 0         21    13    valid / Python OK


mqtt_sensor_bridge.py läuft jetzt als systemd-Service.
Sensorwerte werden dauerhaft per MQTT gesendet.
Node-RED Dashboard zeigt RO- und Mixing-Tank.
State-Machine sendet Prozesszustand per MQTT.
Dashboard zeigt Process State und Aktor-LEDs.
pH, Temperatur und Dissolved Oxygen werden gelesen.
EC wird gelesen, aber noch falsch skaliert dargestellt.


==================================================================

Useful Commands
Check sensor bridge service
systemctl status cds-sensor-bridge.service
Follow sensor bridge logs
journalctl -u cds-sensor-bridge.service -f
Restart sensor bridge after code changes
sudo systemctl restart cds-sensor-bridge.service
Check MQTT sensor topic
mosquitto_sub -h localhost -t cds/status/sensors -v
Check MQTT process topic
mosquitto_sub -h localhost -t cds/status/process -v
Check Python syntax
cd ~/cds_control
source .venv/bin/activate

python -m py_compile mqtt_sensor_bridge.py
python -m py_compile main_water_state_machine.py
python -m py_compile services/mqtt_publisher.py

## 24/06/2026 - Current Project Status

The current implementation provides a Python-based control and monitoring layer for the central dosing process. The system is currently focused on safe water-side validation, sensor integration, MQTT communication, dashboard visualization and preparation of the first controlled fill-and-measure process.

### Implemented Components

#### MQTT Sensor Bridge

A Python-based sensor bridge reads process values from the OPC-UA server and publishes them to the local MQTT broker.

Currently published sensor data includes:

* RO tank level
* Mixing tank level
* EC value
* pH value
* water temperature
* dissolved oxygen

The sensor bridge publishes its data to:

```text
cds/status/sensors
```

The payload contains structured sections for tank levels, water values, actuator placeholders and possible error messages.

The sensor bridge is designed to run as a systemd service and includes reconnect handling for OPC-UA communication issues. It also avoids unnecessary continuous log output and only reports relevant status or error changes.

#### Node-RED Dashboard Integration

The Node-RED dashboard has been extended to consume the Python MQTT payloads and visualize the current process state in a clearer structure.

The dashboard currently displays:

* RO tank level in liters and percent
* Mixing tank level in liters and percent
* Water quality values

  * EC
  * pH
  * water temperature
  * dissolved oxygen
* MQTT bridge status
* last sensor update
* process state machine status
* process errors
* actuator states as LED indicators

A timeout mechanism was added for the sensor bridge. If no new sensor MQTT message is received within the configured timeout, the dashboard switches the sensor bridge state to `STALE`. When new data is received again, the status returns to `RUNNING`.

#### Process State Machine Monitoring

The process state machine publishes its current state and actuator status to:

```text
cds/status/process
```

The dashboard visualizes the current process state and actuator states.

A process timeout handling mechanism was added in Node-RED. Successful process endings can transition to `IDLE`, while error states such as `ERROR` remain visible and are not overwritten by a timeout state. This prevents real errors from being hidden automatically.

#### Preflight Check

A Python preflight check script was added to validate the software and communication environment before running process tests.

The preflight check verifies:

* required project files exist
* Python syntax of important modules is valid
* disk space is sufficient
* GPIO configuration is plausible
* no duplicate GPIO assignments exist
* no GPIO output is initialized or switched during the check
* required systemd services are active
* MQTT broker is reachable
* OPC-UA endpoint is readable
* current MQTT sensor payload is received and structurally valid

The preflight check is read-only with respect to the hardware and does not switch pumps, relays or valves.

#### MQTT Publisher Improvements

The MQTT process publisher was improved to make process status publishing more robust.

The current implementation supports:

* JSON payload publishing
* publish confirmation
* publish timeout handling
* optional fail-soft behavior
* structured process state payloads
* actuator status reporting

This improves reliability for important process states such as `RUNNING`, `FINISHED`, `ERROR` and safe shutdown states.

#### Actuator Manager

An `ActuatorManager` was introduced to centralize the handling of digital outputs.

It provides a cleaner structure for:

* registering actuators
* retrieving actuator objects by name
* collecting actuator status
* switching all registered actuators off safely
* closing all registered outputs

This prepares the project for additional actuators such as circulation pumps, sensor flow pumps and future routing components.

#### Fill and Measure State Machine

A new `FillAndMeasureStateMachine` draft was added as preparation for the first controlled filling process.

The intended process is:

```text
RO water
→ Mixing Tank filling
→ level measurement
→ stop filling at target amount
→ optional water circulation
→ optional sensor circulation
→ water value measurement
```

The first version is focused only on controlled RO filling and measurement preparation. It does not perform chemical dosing, target tank routing or transfer pump control.

The current draft includes:

* configurable process settings
* target fill amount
* maximum fill time
* RO water availability check
* Mixing Tank level check
* safe shutdown on error or keyboard interrupt
* optional placeholders for:

  * mixing circulation pump
  * sensor circulation pump

The circulation pumps are separated conceptually:

```text
mixing_circulation_pump
sensor_circulation_pump
```

This keeps tank mixing and sensor box flow as two different process functions.

### Current Hardware Validation Status

The currently validated water-test outputs are:

* Mixer Refill Pump
* RO supply valve / Valve 6
* Drain valve / Valve 0

The transfer pump is not yet integrated into the Python process logic. Its final electrical connection and control path are still pending hardware clarification.

### Current Design Decision

The next development focus is the basic Mixing Tank process, not recipe execution, peristaltic dosing or target tank routing.

The current priority is:

```text
RO water → Mixing Tank → level-based filling → circulation → measurement
```

Recipe handling, dosing logic, routing logic and cleaning phases will be added later after the basic Mixing Tank process is stable and physically validated.

### Safety Notes

The current implementation is still in validation stage.

Important limitations:

* no chemical dosing is active
* no automatic routing to solution tanks is active
* transfer pump logic is not active
* Mixing Tank level calibration is not final
* water quality values are displayed but not yet process-validated
* circulation pumps are prepared but disabled by default

Before each hardware test, the preflight check should be executed and the physical water path should be verified manually.


## Fill and Measure Preparation

The project currently includes a prepared `FillAndMeasureStateMachine` for controlled RO water filling into the Mixing Tank.

Current process scope:

```text
RO water → Mixing Tank → level-based stop → optional circulation → measurement
```

The process is currently water-only. It does not yet include chemical dosing, target tank routing, transfer pump control or automatic recipe execution.

### Safety Features

Implemented safety mechanisms:

```text
- manual start confirmation
- hardware execution lock
- required confirmation text
- RO water availability check
- Mixing Tank level check
- maximum fill time
- no-fill-progress watchdog
- negative level drift detection
- safe shutdown on error or KeyboardInterrupt
- centralized actuator shutdown via ActuatorManager
```

Hardware execution is blocked by default:

```json
"hardware_execution_enabled": false,
"required_confirmation_text": "confirmed"
```

As long as `hardware_execution_enabled` is `false`, no GPIO output is initialized or switched.

### Fill Progress Watchdog

The filling process stops automatically if the Mixer Refill Pump is active but the Mixing Tank level does not rise plausibly.

Relevant settings:

```json
"min_fill_progress_liters": 1.5,
"no_fill_progress_timeout_seconds": 15.0,
"max_negative_level_drift_liters": 2.0
```

### Sensor Filtering

The Mixing Tank level is filtered before being used for process decisions.

```json
"level_filter_samples": 5,
"target_reached_confirm_samples": 3
```

This reduces the effect of sensor noise and prevents stopping based on a single unstable value.

### Process Logging

Fill-and-measure runs are logged as CSV files in:

```text
logs/
```

The log contains process state, sensor values, filtered level values, added liters, errors and actuator states.

### Current Validation Status

Validated so far:

```text
- preflight check passes
- main_fill_and_measure.py starts correctly
- settings are loaded correctly
- hardware lock blocks unsafe starts
- no GPIO output is switched while hardware_execution_enabled is false
- dashboard displays process errors correctly
```

### Important Rule

Only enable hardware execution on site after the physical RO water path has been verified.

Before running a real test, check:

```text
- RO water path is open
- Valve 6 leads safely to the Mixing Tank
- Mixing Tank can accept the configured volume
- pump is not working against closed valves
- emergency abort is clear
- preflight check passes
```

### Useful Commands

```bash
cd ~/cds_control
source .venv/bin/activate

python preflight_check.py
python main_fill_and_measure.py
```

Expected lock behavior when hardware execution is disabled:

```text
[BLOCKED] Hardware execution is disabled in config/process_settings.json.
```

Recommended `.gitignore` entries:

```gitignore
__pycache__/
*.py[cod]
.venv/
logs/*.csv
```


- Github nur über VsCode und GitLab nur über Bash remote über ssh 
git push gitlab main