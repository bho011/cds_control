"""Kennzeichnung "welche MQTT-Nachricht ist dieser Sensor-Snapshot" (AP-02C).

Rein, ohne MQTT/Hardware-Import. Zwei Signale:

- seq: Empfangszaehler des MqttTopicReader (RX_META_KEY). Unterscheidet
  verschiedene Nachrichten exakt, unabhaengig vom Payload-Inhalt.
- timestamp: Bridge-Zeitstempel (isoformat, Sekundenaufloesung, Uhr der
  Bridge). Dient nur als Ordnungskriterium (strikt spaeter als zuvor).

Grenzen: Beides belegt eine neue NACHRICHT, nicht eine neue PHYSISCHE Messung
(eine am OPC-UA-Server eingefrorene Groesse erscheint weiter als neue
Nachricht). Fehlende oder ungueltige Kennzeichnung gilt als nicht verwendbar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

RX_META_KEY = "_mqtt_rx"

Stamp = tuple[int, datetime]


def read_stamp(snapshot: Any) -> Stamp | None:
    """(seq, timestamp) eines Snapshots oder None, wenn fehlend/ungueltig."""
    if not isinstance(snapshot, dict):
        return None
    meta = snapshot.get(RX_META_KEY)
    seq = meta.get("seq") if isinstance(meta, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None
    raw = snapshot.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        timestamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return seq, timestamp


def is_newer(stamp: Stamp, previous: Stamp | None) -> bool:
    """True nur bei groesserer seq UND strikt spaeterem Zeitstempel."""
    if previous is None:
        return True
    try:
        return stamp[0] > previous[0] and stamp[1] > previous[1]
    except TypeError:  # naive vs. aware: nicht vergleichbar -> nicht neu
        return False
