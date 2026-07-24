# CDS – Konzept für EC-Mischprozess und spätere Korrekturregelung

**Status:** Fachliches Konzept, noch keine Implementierung  
**Zweck:** Verbindliche Arbeitsgrundlage für spätere Firmware-, Python-, State-Machine- und Dashboard-Arbeiten  
**Hinweis:** Vorläufige Prozesswerte müssen bei realen Wasser- und Nährstofftests validiert werden.

---

## 1. Bewusste Scope-Entscheidungen

### Vorläufig nicht Bestandteil

Der Bereich **„DEV Info“ / Diagnostics / Service Mode** wird zunächst nicht erweitert. Konnektivitätstests, Diagnoseanzeigen und Passwortschutz sind sinnvoll, haben aktuell aber geringere Priorität als die sichere Peristaltik- und Mischprozessintegration.

### MCU-Zuordnung

#### MCU-A – pH-Kleindosierung

MCU-A bleibt für einzelne Pumpen zur pH-Korrektur vorgesehen.

- keine Doppelbelegung,
- keine parallelen Pumpenpaare für denselben Stoff,
- Säure und Base niemals gleichzeitig dosieren,
- spätere schrittweise Regelung mit kleinen Mengen, Mischpause und erneuter Messung.

Vorläufige Zuordnung:

| Kanal | Aufgabe |
|---|---|
| P1 | pH-Säure |
| P2 | pH-Base |
| P3 | offen |
| P4 | offen |

#### MCU-B – EC-Nährstoffdosierung

MCU-B nutzt zwei Pumpen je Nährstofflösung. Die Kanalzuordnung folgt der
physischen Hardwareanordnung, nicht der fortlaufenden Kanalnummerierung
(siehe `config/peristaltic_mapping.json`):

| Kanal | Aufgabe |
|---|---|
| P1 | Nährstofflösung A – Pumpe A1 |
| P2 | Nährstofflösung B – Pumpe B1 |
| P3 | Nährstofflösung A – Pumpe A2 |
| P4 | Nährstofflösung B – Pumpe B2 |

Alle Saug- und Druckleitungen bleiben physisch getrennt. Nährstofflösung A und B werden nicht vor dem Mixing Tank in einer gemeinsamen Leitung zusammengeführt.

---

## 2. Grundprinzip der Pumpenpaare

Zwei Pumpen für dieselbe Nährstofflösung sollen die Dosierzeit ungefähr halbieren.

Bei gleicher Förderleistung:

```text
A1 = 50 % der Gesamtmenge A
A2 = 50 % der Gesamtmenge A

B1 = 50 % der Gesamtmenge B
B2 = 50 % der Gesamtmenge B
```

Nach der realen Kalibrierung wird die Verteilung proportional zur individuellen Förderleistung vorgenommen:

```text
Menge Pumpe 1 =
Gesamtmenge × Förderleistung Pumpe 1
              -----------------------------------------
              Förderleistung Pumpe 1 + Förderleistung Pumpe 2
```

Die zweite Pumpenmenge wird als Restmenge berechnet.

### Sicherheitsregel bei Pumpenfehler

Fällt eine der vier MCU-B-Pumpen während der Nährstoffdosierung aus:

- alle vier Nährstoffpumpen stoppen,
- keinen Restauftrag automatisch auf eine andere Pumpe übertragen,
- Dosierschritt als unvollständig markieren,
- Prozess in einen sicheren Pause- oder Fehlerzustand versetzen.

Solange keine Durchfluss- oder Gewichtsmessung vorhanden ist, kennt die Software nur die rechnerisch dosierte Menge aus Schritten und Kalibrierwerten, nicht die sicher gemessene reale Fördermenge.

---

## 3. Verbindliche Volumengrenzen

| Größe | Wert | Bedeutung |
|---|---:|---|
| Physische Tankkapazität | ca. 200 l | Füllung bis zum Rand, kein zulässiger Prozesswert |
| Maximale RO-Rezeptmenge | 180 l | reine RO-Wassermenge |
| Maximales Prozessvolumen | 185 l | harte Grenze für automatische Prozesse |
| Unbenutzte Sicherheitsreserve | ca. 15 l | Bereich zwischen 185 und 200 l |
| Globales RO-Korrekturbudget | max. 50 l | zusätzlich durch Restkapazität begrenzt |

Die oberen ca. 15 Liter bis zum physischen Rand bleiben bewusst ungenutzt, unter anderem für:

- Wellenbewegung,
- Zirkulation,
- Nachlauf,
- Messabweichungen,
- Schaumbildung,
- sonstige Prozessunsicherheiten.

Die tatsächlich verfügbare Korrekturreserve lautet:

```text
verfügbare Korrekturreserve =
185 l - aktuelles beziehungsweise geschätztes Prozessvolumen
```

Die GUI soll warnen, wenn nach RO-Wasser und Nährstofflösungen nur wenig Korrekturreserve verbleibt. Diese Warnung blockiert das Rezept vorerst nicht automatisch.

---

## 4. Vorläufiger Mixing-Prozess

Die Werte 30 l, 50 %, 60 s und drei EC-Prüfpunkte sind zunächst konfigurierbare Startwerte und keine endgültig validierten Produktionsparameter.

### Phase 1 – Validierung

Vor Prozessstart prüfen:

- gültiger und unveränderlicher RunConfig-Snapshot,
- gültiges Rezept,
- ausreichender RO-Wasservorrat,
- aktueller Tankfüllstand plausibel,
- maximal 185 l Prozessvolumen,
- Sensorwerte vorhanden und nicht veraltet,
- Sensor- und Mixing-Tank-Zirkulationspumpe verfügbar,
- MCU-B verbunden und eindeutig identifiziert,
- alle vier MCU-B-Pumpen im Zustand `IDLE`,
- keine aktive Störung oder Emergency-Stop-Sperre,
- alle Nährstoffmengen und Pumpenpaar-Zielmengen berechnet.

### Phase 2 – RO-Vorbefüllung

```text
RO-Befüllung starten
→ Mixing Tank bis vorläufig 30 l befüllen
```

Die 30-l-Schwelle muss konfigurierbar bleiben und später am realen System bestätigt werden, insbesondere wegen der bekannten Unsicherheit des Füllstandsensors im unteren Messbereich.

### Phase 3 – Zirkulation starten

Bei Erreichen der Vorfüllschwelle:

```text
Sensorzirkulation EIN
Mixing-Tank-Zirkulationspumpe EIN
```

Danach kurze konfigurierbare Vorlaufzeit, damit:

- die Strömung stabil wird,
- die Sensorbox vollständig durchspült wird,
- keine stehende Altflüssigkeit bewertet wird.

### Phase 4 – Nährstoffdosierung starten

Alle vier MCU-B-Pumpen starten gemeinsam:

```text
A1 + A2 → Nährstofflösung A
B1 + B2 → Nährstofflösung B
```

Nährstofflösung A und B werden entsprechend dem Rezeptverhältnis dosiert, normalerweise 50/50, aber beispielsweise auch 40/60.

### Phase 5 – RO-Befüllung fortsetzen

Bei vorläufig 50 % Fortschritt der gesamten Nährstoffdosierung:

```text
RO-Befüllung wieder aufnehmen
```

Ziel:

- verbleibende konzentrierte Nährstofflösung fortlaufend verdünnen,
- Homogenisierung unterstützen,
- lokale Konzentrationsspitzen reduzieren.

Der Fortschritt sollte aus den geplanten und rechnerisch abgeschlossenen Pumpenschritten aller vier MCU-B-Pumpen abgeleitet werden.

### Phase 6 – Auf beide parallelen Teilprozesse warten

Der nächste Zustand darf erst erreicht werden, wenn beide Bedingungen erfüllt sind:

```text
RO-Auftrag abgeschlossen
UND
Nährstoffauftrag A/B abgeschlossen
```

Der finale Vor-Korrektur-Füllstand entspricht näherungsweise:

```text
RO-Sollmenge
+ Nährstofflösung A
+ Nährstofflösung B
```

Bei paralleler Befüllung müssen Gesamtfüllstand, geschätzte RO-Menge und rechnerisch eingebrachte Nährstoffmenge getrennt bilanziert werden.

### Phase 7 – Nachmischen

Nach Abschluss aller Füllaufträge:

```text
RO-Pumpe AUS
MCU-B-Pumpen IDLE
Mixing-Tank-Zirkulation EIN
Sensorzirkulation EIN
Nachmischen für vorläufig 60 Sekunden
```

Die Mischzeit bleibt konfigurierbar und wird später anhand realer Messwertstabilität bewertet.

---

## 5. EC-Messstrategie

Nicht lediglich drei einzelne Sensorwerte verwenden.

### Drei stabilisierte Prüffenster

Für jeden Prüfpunkt:

1. mehrere EC-Werte über ein kurzes Zeitfenster erfassen,
2. veraltete oder unplausible Werte verwerfen,
3. Median oder robusten Mittelwert berechnen,
4. Temperatur mitprotokollieren.

Anschließend aus drei Prüfpunkten den Bewertungswert bilden.

Beispiel:

```text
Prüfpunkt 1: 2,31 mS/cm
Prüfpunkt 2: 2,34 mS/cm
Prüfpunkt 3: 2,33 mS/cm

Bewertungswert: ca. 2,33 mS/cm
```

### Stabilitätsprüfung

Zusätzlich zum Mittelwert prüfen:

```text
maximaler Prüfpunkt - minimaler Prüfpunkt
<= zulässige Schwankung
```

Ist die Streuung zu groß:

```text
weiter mischen
→ erneut messen
```

### Temperaturbezug

Vor Implementierung klären:

- Liefert der vorhandene Sensor bereits temperaturkompensiertes `EC25`?
- Welche Kompensationsmethode nutzt Sensor oder Transmitter?
- Welcher Temperaturkoeffizient ist eingestellt?

Es darf keine doppelte Temperaturkompensation erfolgen.

Der spätere Rezept-Sollwert sollte eindeutig auf eine Referenztemperatur, voraussichtlich 25 °C, bezogen werden.

---

## 6. Toleranzbereich

Der EC-Sollwert wird nicht als punktgenau zu erreichender Einzelwert behandelt.

```text
Untergrenze = EC-Sollwert - Toleranz
Obergrenze  = EC-Sollwert + Toleranz
```

Die konkrete Toleranz wird noch nicht festgelegt. Sie muss anhand realer Daten bestimmt werden:

- Sensorrauschen,
- Temperaturverhalten,
- Mischdauer,
- Pumpengenauigkeit,
- Prozessanforderungen,
- Wiederholbarkeit mehrerer Chargen.

Die Toleranz gehört voraussichtlich in die technische Prozesskonfiguration und nicht zwingend in jedes Rezept.

---

## 7. Rechnerische RO-Korrektur bei zu hohem EC

### Allgemeine Verdünnungsformel

Unter vereinfachter Stoffbilanz:

```text
EC_ziel =
(EC_aktuell × V_aktuell + EC_RO × DeltaV)
------------------------------------------------
(V_aktuell + DeltaV)
```

Nach der benötigten RO-Menge aufgelöst:

```text
DeltaV =
V_aktuell × (EC_aktuell - EC_ziel)
            ---------------------------------
            (EC_ziel - EC_RO)
```

Falls `EC_RO` näherungsweise 0 mS/cm ist:

```text
DeltaV =
V_aktuell × (EC_aktuell / EC_ziel - 1)
```

### Wichtige Einschränkung

Die Formel ist nur ein theoretischer Startwert. Leitfähigkeit und Konzentration sind bei realen Nährstoffmischungen nicht unter allen Bedingungen exakt linear.

Daher immer:

```text
berechnen
→ konservativ begrenzen
→ RO-Wasser zuführen
→ mischen
→ stabil messen
→ neu berechnen
```

### Begrenzung eines Korrekturschrittes

Die tatsächlich zulässige RO-Zugabe ist das Minimum aus:

- theoretisch berechneter Bedarf,
- verbleibende Kapazität bis 185 l,
- verbleibendes kumulatives 50-l-Korrekturbudget,
- maximaler konfigurierter Einzelkorrekturschritt,
- gegebenenfalls konservativer Sicherheitsfaktor.

Ein pauschaler 5-l-Schritt kann als vorläufige Obergrenze untersucht werden, ist aber nicht für jede Situation geeignet.

### Warnung bei fehlender Reserve

Beispiel:

```text
RO-Wasser:                  180,0 l
Nährstofflösungen:            1,8 l
Vor-Korrektur-Volumen:      181,8 l
Restkapazität bis 185 l:      3,2 l
```

Die Automatik darf maximal 3,2 l ergänzen. Die übrigen ca. 15 l bis zum physischen Rand bleiben ungenutzt.

---

## 8. EC zu niedrig

Eine spätere automatische Nährstoffkorrektur ist grundsätzlich möglich, benötigt jedoch zwei getrennte Grundlagen:

1. Kalibrierung jeder Peristaltikpumpe:
   - ml pro Schritt,
   - Förderleistung,
   - Mindestmenge,
   - Wiederholgenauigkeit.

2. Empirische Prozessantwort:
   - Wie stark steigt der EC-Wert bei einer bestimmten Zusatzmenge?
   - Abhängigkeit von Tankvolumen, Temperatur, Rezept und aktuellem EC-Bereich.

Mögliche spätere Prozesskennzahl:

```text
G =
DeltaEC × Tankvolumen
---------------------
dosierte Nährstoffmenge
```

Mögliche Schätzung:

```text
zusätzliche Nährstoffmenge =
(EC_ziel - EC_aktuell) × Tankvolumen
------------------------------------
G
```

Die Zusatzmenge muss anschließend aufgeteilt werden:

```text
Gesamtmenge
→ Verhältnis A/B
→ Pumpenpaar A1/A2
→ Pumpenpaar B1/B2
```

### Entscheidung für die erste reale Version

```text
EC zu hoch:
rechnerisch unterstützte RO-Korrektur möglich

EC zu niedrig:
zunächst Prozess pausieren und Bedienerentscheidung anfordern
```

Die automatische Unterkorrektur folgt später auf Basis realer Kalibrier- und Chargenlogs.

---

## 9. Abbruchbedingungen und Korrekturschleifen

Die konkrete Parametrierung wird erst nach realen Tests festgelegt.

Die Architektur muss später folgende Grenzen unterstützen:

- maximale Anzahl Korrekturzyklen,
- maximale kumulierte RO-Korrektur,
- maximale kumulierte Nährstoffkorrektur,
- maximale Korrekturdauer,
- maximales Prozessvolumen 185 l,
- maximales zulässiges Sensorrauschen,
- Abbruch bei veraltetem oder fehlendem Sensorwert,
- Abbruch bei Pumpen- oder Kommunikationsfehler,
- Abbruch, wenn eine Korrektur keine plausible Wirkung zeigt.

Keine Endlosschleifen.

---

## 10. Verhalten bei erfolgloser Automatik

Wenn:

- das maximale Prozessvolumen erreicht ist,
- die Korrekturreserve nicht ausreicht,
- Messwerte instabil bleiben,
- oder die Korrektur keine plausible Wirkung zeigt,

dann:

```text
Automatik pausieren
alle Dosier- und RO-Pumpen AUS
sicherer Zustand
Bedienerentscheidung erforderlich
```

Mögliche Entscheidungen müssen getrennt bleiben:

1. **Manuelle Bearbeitung übernehmen**
2. **Prozess abbrechen**
3. **Drain ausdrücklich starten**
4. später eventuell **Charge außerhalb Toleranz akzeptieren**, falls fachlich zulässig

Ein Prozessabbruch darf niemals automatisch einen Drain auslösen.

---

## 11. Manuelle Bearbeitung und späterer Transfer

Bei manueller Bearbeitung:

```text
MANUAL_HOLD
```

Nach manueller Korrektur darf die Mischung nicht automatisch in einen Solution Tank übertragen werden.

Erforderliche spätere Freigabekette:

```text
manuelle Korrektur abgeschlossen
→ erneut mischen
→ abschließende EC-/pH-Prüfung
→ Bediener akzeptiert Charge
→ Transfer zum Solution Tank freigeben
```

Die konkrete Transferlogik ist ein späteres Arbeitspaket.

---

## 12. Reihenfolge EC und pH

Die pH-Phase über MCU-A folgt erst nach Abschluss der EC-Phase, weil die Nährstoffzugabe den pH-Wert beeinflussen kann.

Langfristiger Ablauf:

```text
EC-Phase abschließen
→ pH-Phase durchführen
→ final mischen
→ EC und pH abschließend prüfen
→ Charge freigeben
→ Transfer
```

Nach der pH-Korrektur muss EC erneut geprüft werden, weil Säure oder Base zusätzliche Ionen einbringen und den Leitwert verändern können.

---

## 13. Logging

Für jede Charge mindestens protokollieren:

- Batch-ID,
- Recipe-ID und vollständiger RunConfig-Snapshot,
- Start- und Endzeit,
- RO-Sollmenge,
- geschätzte RO-Istmenge,
- Nährstoff-Sollmengen A/B,
- Einzelziele A1/A2/B1/B2,
- Pumpenschritte und Laufzeiten,
- Firmwareantworten,
- Füllstandsverlauf,
- EC-Rohmessungen,
- EC-Prüfpunkte,
- Bewertungswert und Streuung,
- Temperatur,
- Toleranzbereich,
- jede RO-Korrektur,
- spätere Nährstoffkorrekturen,
- Mischzeiten,
- Fehler und Unterbrechungen,
- Bedienerentscheidungen,
- finaler Status.

Ziel der Logs:

- Mischzeiten optimieren,
- 30-l-Vorfüllschwelle bewerten,
- 50-%-RO-Wiederaufnahme bewerten,
- reale Wirkung von RO-Korrekturen bestimmen,
- Pumpenabweichungen erkennen,
- realistischen Toleranzbereich festlegen,
- spätere automatische Unterkorrektur entwickeln.

---

## 14. Vorläufige State Machine

```text
VALIDATE_RUN
PRE_FILL_RO
START_CIRCULATION
START_NUTRIENT_DOSING
RESUME_RO_FILL
WAIT_FOR_RO_AND_NUTRIENTS
POST_MIX
SAMPLE_EC
EVALUATE_EC

EC_IN_RANGE
→ COMPLETE_EC_PHASE

EC_TOO_HIGH
→ CALCULATE_RO_CORRECTION
→ ADD_RO_CORRECTION
→ POST_CORRECTION_MIX
→ SAMPLE_EC

EC_TOO_LOW
→ MANUAL_DECISION
oder später NUTRIENT_CORRECTION

LIMIT_REACHED
→ MANUAL_DECISION

MANUAL_HOLD
FAULT
SAFE_STOP
ABORTED
COMPLETE
```

Die endgültige State Machine wird erst nach Firmwarekommunikation, Mock-Tests und Wasser-Kalibrierung implementiert.

---

## 15. Noch offene Entscheidungen

Vor einer produktiven Implementierung klären:

1. Reale MCU-A-/MCU-B-Pumpenzuordnung.
2. Kalibrierwerte jeder einzelnen Pumpe.
3. Genauer EC-Temperaturbezug und vorhandene Sensor-Kompensation.
4. Größe und Dauer eines EC-Messfensters.
5. Abtastrate und Ausreißerbehandlung.
6. Zulässige Schwankung zwischen den drei Prüfpunkten.
7. EC-Toleranzbereich.
8. Maximaler RO-Einzelkorrekturschritt.
9. Konservativer Faktor für die theoretische RO-Korrektur.
10. Maximale Anzahl und Dauer der Korrekturzyklen.
11. Verhalten bei EC-Unterkorrektur nach Vorliegen realer Logs.
12. Endgültige Transfer- und Qualitätsfreigabelogik.
13. Reale Prüfung der 30-l-Vorfüllschwelle.
14. Reale Prüfung der 50-%-Fortsetzungsschwelle.
15. Reale Prüfung der 60-s-Nachmischzeit.

---

# Wiederverwendbarer Prompt für eine spätere Implementierungsplanung

```text
Arbeite auf Basis von docs/EC_MIXING_PROCESS_CONCEPT.md und beachte CLAUDE.md.

Ziel dieser Aufgabe ist ausschließlich die technische Planung des dort beschriebenen
EC-Mischprozesses. Noch keine Hardware aktivieren, keine Firmware flashen, keine reale
Dosierung durchführen und keinen Commit oder Push erzeugen.

1. Lies den aktuellen CDS-Code, das Recipe-/RunConfig-Modell, die State Machine,
   ProcessController, Sensoranbindung und Peristaltik-Firmware vollständig.
2. Gleiche den Ist-Zustand mit EC_MIXING_PROCESS_CONCEPT.md ab.
3. Erstelle einen schrittweisen Implementierungsplan für:
   - MCU-B mit zwei Pumpen für A und zwei Pumpen für B,
   - getrennte Leitungen,
   - RO-Vorfüllung,
   - parallele Zirkulation und Nährstoffdosierung,
   - RO-Wiederaufnahme bei konfigurierbarem Nährstofffortschritt,
   - stabiles EC-Sampling,
   - theoretische und konservativ begrenzte RO-Korrektur,
   - Manual-Hold- und Safe-Stop-Zustände,
   - Batch-Logging.
4. Erfinde keine Kalibrierwerte, Toleranzen, Sensorparameter oder Zeitlimits.
5. Markiere alle Werte, die erst durch Wasser-, Kalibrier- oder reale Chargentests
   bestimmt werden können.
6. Trenne den Plan in:
   - Firmwareprotokoll,
   - Python-Serial-Client mit Mock,
   - Pumpenkalibrierung,
   - State-Machine-Erweiterung,
   - Dashboard,
   - Logging,
   - Hardwaretests.
7. Gib Risiken, Abhängigkeiten, Teststrategie und Akzeptanzkriterien an.
8. Nimm noch keine Codeänderungen vor.
```
