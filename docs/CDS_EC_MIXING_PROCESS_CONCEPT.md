# CDS – Konzept für EC-Mischprozess und spätere Korrekturregelung

**Status:** Fachliches Konzept; Teile sind umgesetzt (siehe die Nachträge vom 2026-10-14 und 2026-10-18)\
**Zweck:** Verbindliche Arbeitsgrundlage für spätere Firmware-, Python-, State-Machine- und Dashboard-Arbeiten  
**Hinweis:** Vorläufige Prozesswerte müssen bei realen Wasser- und Nährstofftests validiert werden.

---

## Nachtrag 2026-10-14 – Verbindlicher Umfang AP-07 (Scope-Entscheidung)

Dieser Nachtrag ersetzt keinen Abschnitt dieses Dokuments. Die Abschnitte 1 (MCU-A), 11, 12 und 14 beschreiben den **ursprünglichen, umfangreicheren Konzeptstand** und bleiben als **historischer Plan** unverändert erhalten. Wo sie vom Nachtrag abweichen, gilt der Nachtrag.

**Entscheidung:** AP-07 wird für die AP2-Projektarbeit bewusst reduziert. Die automatische pH-Korrektur über MCU_A war eine Erweiterung der eigenen ursprünglichen Konzeption und keine verbindliche Projektanforderung.

**Verbindlicher Umfang AP-07:**

- pH-Messwerte aus der bestehenden Sensorintegration nutzen,
- pH-Istwert gegen den Rezept-Sollwert bewerten (umgesetzt mit AP-06, `statemachine/measurement_evaluation.py`),
- bei Abweichung außerhalb der konfigurierten Toleranz (`ph_tolerance` in den Prozesseinstellungen) den Prozess kontrolliert beenden, alle Aktoren abschalten und einen Bedienereingriff erzwingen (State `ERROR`, Quittierung über `acknowledge_error`),
- Fehler und Bewertungsstatus im vorhandenen Dashboard und im Prozesslog (`evaluation_status`, `evaluation_details`) darstellen.

**pH-Zulässigkeitsband (Ausnahme, bewusst entschieden):** Statt einer symmetrischen Toleranz um den Rezept-Sollwert kann für pH ein Band `ph_min`/`ph_max` in den Prozesseinstellungen gesetzt werden (festgelegt: 5,0 bis 6,5, Optimum 6,0 entsprechend dem Rezept-Sollwert; beide Grenzen gelten noch als zulässig). Hinweis: `config/process_settings.json` enthält derzeit abweichende Testwerte (`ph_max` 8,9), weil der pH-Sensor defekt ist und noch kein neuer beschafft wurde. Regeln, alle fail-closed: beide Grenzen sind Pflicht, `0 <= ph_min < ph_max <= 14`, `ph_tolerance` darf nicht zusätzlich gesetzt sein, ein fehlender oder unplausibler Istwert führt zu `ERROR`. Der Rezept-Sollwert wird dann nur angezeigt und entscheidet nicht. Ohne `ph_min`/`ph_max` und ohne `ph_tolerance` bleibt pH `NOT_EVALUATED`. Das Band ist eine reine Bewertung, keine Regelung und keine Dosierung.

**Ausdrücklich nicht Bestandteil von AP2 (spätere Erweiterung außerhalb des AP2-Umfangs):**

- automatische Säure-/Base-Dosierung über MCU_A,
- MCU_A-Kalibrierung,
- pH-Korrekturschleifen (schrittweise Dosierung, Mischpause, erneute Messung).

Diese Punkte sind nicht verworfen, werden aber im AP2-Umfang weder entwickelt noch als vorhanden dargestellt.

**Abgrenzung der drei Zustände (nicht austauschbar):**

| Zustand | Bedeutung | Status im Code | Prozessfolge |
|---|---|---|---|
| `ERROR` (Bewertungsstatus `FAILED`) | Istwert außerhalb der Toleranz, oder konfigurierte Toleranz ungültig, oder Sollwert/Istwert fehlt bzw. ist unplausibel (fail-closed) | umgesetzt (`FillAndMeasureState.ERROR`, Grund-Codes z. B. `ph_out_of_tolerance`, `tolerance_invalid`, `setpoint_invalid`, `measurement_implausible`) | Fehler wird verriegelt, Aktoren aus, Bedienerquittierung erforderlich; keine Korrektur, kein automatischer Drain |
| `MANUAL_HOLD` | Konzeptzustand für geordnete manuelle Bearbeitung mit späterer Wiederaufnahme (Abschnitte 11 und 14) | **nicht implementiert**, kein Code, kein Test | Wird nicht durch `ERROR` ersetzt oder vorgetäuscht; `ERROR` ist ein Abbruch ohne Wiederaufnahme im laufenden Prozess |
| `NOT_EVALUATED` | Für EC und/oder pH ist **keine** Toleranz konfiguriert; die Größe wird nicht bewertet. Es gibt bewusst keine Standardtoleranz | umgesetzt (`STATUS_NOT_EVALUATED`) | Prozess endet regulär (`FINISHED`), die Sollwerterfüllung gilt aber **nicht** als bestätigt; das Dashboard zeigt „Sollwerterfüllung nicht bestätigt“. Kein Fehler, kein Stopp |

Folge für den Betrieb: Ohne gesetzte `ph_tolerance` erfolgt keine pH-Abschaltung. Die Wirksamkeit der pH-Überwachung hängt davon ab, dass die Toleranz fachlich festgelegt und in den Prozesseinstellungen gesetzt wird (siehe Abschnitt 15, Punkt 7 für EC; für pH analog offen).

---

## Nachtrag 2026-10-18 – Umsetzungsstand AP-05 (parallele Dosierung, überlappende RO-Befüllung, EC-Toleranz)

Dieser Nachtrag ersetzt keinen Abschnitt dieses Dokuments; wo er von den Abschnitten 4, 5, 6 und 14 abweicht, gilt der Nachtrag. Alle genannten Werte sind **vorläufig und nicht hardwarevalidiert**. Umgesetzt ist die Software (hardwarefreie Tests, Branch `feature/ap05-parallel-overlap`); ein beaufsichtigter Hardwaretest ausschließlich mit Wasser steht aus (siehe „Offene Punkte“).

**Parallele Dosierung (Phase 4).**

- Rundenbasiert: Runde *r* ist der *r*-te Chunk jeder Pumpe, die noch Aufträge hat. Alle Chunks einer Runde werden nacheinander gestartet, ohne auf den Abschluss der anderen zu warten (P1 + P3 = Lösung A, P2 + P4 = Lösung B; alle vier Pumpen können gleichzeitig laufen). Die nächste Runde beginnt erst, wenn jede Pumpe der Runde mit `DONE` und anschließendem `IDLE` bestätigt ist. Ein einziger Dosier-Thread bedient den Serial-Client. Die gewichtete Mengenverteilung, die Chunk-Planung und die Grenze von höchstens 10 ml je `DOSE`-Befehl bleiben unverändert.
- Pumpengruppen: Die Startgruppe (Pumpen mit Aufträgen) muss eine erlaubte Gruppe sein (`{P1,P3}`, `{P2,P4}` oder alle vier), sonst wird der Start vor jeder Hardwareausgabe blockiert. In Folgerunden arbeiten nur Teilmengen weiter (Rückbau der Startgruppe); es kommt nie eine Pumpe hinzu, eine fertige Pumpe wird nie erneut gestartet. Die Regeln für manuelle Paralleltests sind unverändert.
- Fehlerfall (Pumpenausfall, Firmware-Timeout, Verbindungsabbruch, Stopp, Emergency Stop): `STOPALL` stoppt alle vier Pumpen; alle zu diesem Zeitpunkt gestarteten, nicht bestätigten Chunks gelten als unsicher (obere Schranke), werden nie wiederholt und nie auf eine andere Pumpe übertragen (Abschnitt 2).

**RO-Wiederaufnahme und Nachmischen (Phasen 5 bis 7).**

- Einstellung `nutrient_ro_resume_progress` (vorläufig 0,5; steht nur in `config/process_settings.json`; erlaubt ist eine Zahl größer 0 bis höchstens 1; ein fehlender oder ungültiger Wert blockiert einen Dosierlauf vor jeder Hardwareausgabe). Maßgeblich ist der per `DONE` und `IDLE` bestätigte Anteil der Gesamtmenge. Der Wert 1,0 entspricht dem bisherigen Ablauf ohne Überlappung (RO erst nach vollständiger Dosierung).
- Bei Erreichen der Schwelle startet die RO-Pumpe nach erneuter Sicherheitsprüfung zusätzlich (Zustand `DOSING_AND_RO_FILL`): neue Messung, nichts unsicher, Pegel nicht unter Phasenbeginn abzüglich erlaubter Drift, gültige Volumenbilanz, ausreichender RO-Vorrat, Dosierung gesund. Die Dosierung läuft weiter; beide Zirkulationen bleiben aktiv. Die RO-Pumpe wird je Lauf höchstens zweimal eingeschaltet (Vorfüllung und eine Wiederaufnahme); eine Nachfülllogik gibt es nicht.
- Dynamisches Abschaltziel: RO-Sollmenge + bestätigte + noch in Ausführung befindliche Nährstoffmenge (`volume_balance.dynamic_phase_target_l`). Die RO-Pumpe schaltet beim ersten ungefilterten Wert darüber sofort ab (Pumpe zuerst aus) und wird nie wieder eingeschaltet. Dadurch schaltet sie nicht zu früh ab; im ungünstigsten Fall liegt sie um die Menge der gerade laufenden Chunks (höchstens 4 × 10 ml) darüber.
- Ein erreichtes Zwischenziel ist **kein** erfolgreicher Abschluss. Nachgemischt wird erst, wenn die Dosierung vollständig bestätigt ist (nichts unsicher, nichts in Ausführung) **und** das endgültige Ziel (RO-Sollmenge + gesamte bestätigte Menge) bei ausgeschalteter RO-Pumpe mit den üblichen Bestätigungsmessungen bestätigt wurde (Schlussbilanz). Wird das Ziel nicht erreicht, endet der Lauf kontrolliert in `ERROR`; es wird nicht nachgefüllt.
- Volumenüberwachung: In jedem Takt wird Mixerstand + noch nicht bestätigte Nährstoffmenge gegen 185 l geprüft. Messfrische (5 s), Plausibilität, RO-Vorrat und der Fortschrittswächter laufen in beiden Teilprozessen weiter; der Fortschrittswächter rechnet das seit der Wiederaufnahme bestätigte Nährstoffvolumen heraus, damit es eine stehende RO-Pumpe nicht verdeckt. Fällt die Dosierung aus, schaltet die State Machine RO und Zirkulationen ab; der Dosierer meldet seinen Abbruch sofort als `STOPPING`, nicht erst nach `STOPALL` und Schließen der Verbindung.

**EC-Toleranz (Abschnitte 6 und 15, Punkt 7).** `ec_tolerance_ms_cm` = 0,10 mS/cm ist als **vorläufiger historischer Referenzwert** aus der bisherigen Node-RED-Steuerung (`EC_config.maxdelta`) in `config/process_settings.json` übernommen, nicht als hardwarevalidierte Qualitätstoleranz. In der alten Steuerung entschied der Wert, ob korrigiert wird; hier ist er die Abnahmegrenze der Endbewertung. Außerhalb der Toleranz endet der Lauf in `ERROR` mit Bedienereingriff; es gibt weder Nachdosierung noch RO-Korrektur noch eine EC-Regelung.

- EC wird nur bei Rezeptläufen mit aktivierter Nährstoffdosierung bewertet. Ein reiner Wasserlauf erhält für EC `NOT_EVALUATED` (Grund `no_nutrient_dosing`): kein Fehler, aber auch keine bestätigte Sollwerterfüllung; die Transferfreigabe verlangt dafür weiterhin die gesonderte Bestätigung. Die pH-Bewertung ist unverändert.
- Exakter Dezimalvergleich, Grenzen eingeschlossen, keine Zusatztoleranz: bei Soll 1,50 und Toleranz 0,10 sind 1,400, 1,500 und 1,600 innerhalb, 1,399 und 1,601 außerhalb. (Gleitkommazahlen hätten 1,6 − 1,5 als 0,10000000000000009 bewertet.)
- Eine konfigurierte, aber ungültige Toleranz (0, negativ, `None`, NaN, Text, Wahrheitswert) blockiert einen Dosierlauf vor jeder Hardwareausgabe mit derselben Prüfregel wie die Endbewertung. Fehlt der Schlüssel, ist das bei einem Dosierlauf ebenfalls ein Konfigurationsfehler (Start blockiert, vor jeder Hardwareausgabe); ein reiner Wasserlauf braucht ihn nicht und erhält EC `NOT_EVALUATED`. Eine Standardtoleranz gibt es weiterhin nicht.

**Offene Punkte (bewusst nicht umgesetzt).**

- Die Rezept-Mischzeit `ec_mixing_time_seconds` ist nicht an die Ausführung angebunden. Das Nachmischen dauert `mixing_seconds` aus `config/process_settings.json` (derzeit 30 s; Konzept: vorläufig 60 s).
- Die EC-Endbewertung beruht auf einer einzelnen Messung. Die Mehrfenster-Stabilitätsmessung aus Abschnitt 5 (drei Prüffenster, Median, Streuung) fehlt. Bei ±0,10 mS/cm sind Fehlalarme möglich (sichere Richtung: Fehler statt falschem OK).
- Der Temperaturbezug des EC-Messwerts (EC25) ist weiterhin ungeklärt (Abschnitte 5 und 15).
- Es gibt keine automatische EC-Nachregelung und keine pH-Korrektur.
- Vor jeder produktiven Nutzung ist ein beaufsichtigter Hardwaretest ausschließlich mit Wasser nötig (gleichzeitiger Betrieb aller vier Motoren, Überlappung, Abschaltverhalten). Ein zusätzliches Software-Gate gibt es nicht; `hardware_execution_enabled` und die vorhandenen Prüfungen bleiben die einzige Freigabe.
- Bedienvoraussetzung für einen Dosierlauf: Mixing- und Sensorzirkulation müssen aktiv sein. `enable_mixing_circulation` steht in `config/process_settings.json` derzeit auf `false` (hardwarewirksam, wird nicht automatisch umgeschaltet) und muss bewusst auf `true` gesetzt werden; die Sensorzirkulation wird im Startdialog gewählt. Andernfalls blockiert der Start mit einer entsprechenden Meldung.
- `max_fill_seconds` (derzeit 236 s) begrenzt jede RO-Phase ab ihrem Beginn, also auch die parallel zur Dosierung laufende. Für große Rezepte muss der Wert zur tatsächlichen RO-Förderrate passen.

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

**Stand 2026-10-18:** Als vorläufiger Startwert ist `ec_tolerance_ms_cm` = 0,10 mS/cm in `config/process_settings.json` gesetzt (historischer Referenzwert aus der bisherigen Steuerung, siehe Nachtrag 2026-10-18). Die Bestimmung aus realen Daten (Liste oben) bleibt offen.

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
7. EC-Toleranzbereich. (Vorläufig 0,10 mS/cm gesetzt, nicht validiert; siehe Nachtrag 2026-10-18.)
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
