"""
Zielvertrags-Tests für services/mqtt_topic_reader.py::MqttTopicReader
(die vereinheitlichte Klasse aus Modularisierungs-Plan Phase 3).

Geprüft werden die drei Kategorien aus dem Plan (Phase 3, Schritt 2):
identisch gebliebenes Verhalten, bewusste kleine Deltas, und der neue
gemeinsame Vertrag (Sentinel-basiertes get_latest()). Kein Netzwerkkontakt.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from services.mqtt_topic_reader import MqttTopicReader


def _message(payload: bytes | str) -> SimpleNamespace:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return SimpleNamespace(payload=payload)


# --- bleibt identisch --------------------------------------------------------


def test_on_connect_subscribes_to_configured_topic_with_qos_zero() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="cds/status/sensors")
    calls: list[tuple[str, int]] = []

    class _FakeClient:
        def subscribe(self, topic, qos):
            calls.append((topic, qos))

    reader._on_connect(_FakeClient(), None, {}, 0)
    assert calls == [("cds/status/sensors", 0)]


def test_wait_for_first_snapshot_reproduces_old_sensor_snapshot_reader_behavior() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t")
    assert reader.wait_for_first_snapshot(timeout_seconds=0.05) is False

    reader._on_message(None, None, _message(json.dumps({"a": 1})))

    assert reader.wait_for_first_snapshot(timeout_seconds=0.05) is True


# --- bewusst geändert (Deltas) ------------------------------------------------


def test_get_latest_always_returns_a_copy_never_the_internal_reference() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t", max_age_seconds=100.0)
    payload = {"a": 1}
    reader._on_message(None, None, _message(json.dumps(payload)))

    returned = reader.get_latest()
    returned["a"] = 999

    assert reader.get_latest()["a"] == 1


def test_locking_and_error_tracking_are_always_available() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t")
    assert hasattr(reader, "_lock")
    assert reader.get_error() is None
    reader.clear()  # darf ohne vorherige Nachricht nicht raisen


def test_on_message_catches_broad_exceptions_not_just_json_decode_error() -> None:
    """Vorher (SensorSnapshotReader) fing nur json.JSONDecodeError - eine
    ungültige Byte-Sequenz hätte dort unbehandelt durchgeschlagen. Die
    vereinheitlichte Klasse fängt breiter und setzt stattdessen _error."""
    reader = MqttTopicReader(host="localhost", port=1883, topic="t")

    class _BadPayload:
        def decode(self, *args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    reader._on_message(None, None, SimpleNamespace(payload=_BadPayload()))

    assert reader.get_latest() is None
    assert reader.get_error() is not None


def test_start_failure_sets_error_instead_of_raising() -> None:
    """Vorher (SensorSnapshotReader.start()) hätte ein Verbindungsfehler
    die Exception direkt weitergereicht - process/water_cycle.py (der
    einzige Aufrufer) ruft start() ohne eigenes Try/Except auf und muss
    deshalb künftig get_error() prüfen, siehe Phase 3, Schritt 2."""
    reader = MqttTopicReader(host="localhost", port=1883, topic="t")

    def _boom(*args, **kwargs):
        raise OSError("connection refused")

    reader.client.connect = _boom

    reader.start()  # darf NICHT raisen

    assert reader.get_error() is not None


# --- neuer gemeinsamer Vertrag: Sentinel-basiertes get_latest() -------------


def test_get_latest_without_argument_uses_instance_max_age_seconds() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t", max_age_seconds=1.0)
    reader._on_message(None, None, _message(json.dumps({"a": 1})))
    reader._latest_received_at = time.monotonic() - 10.0

    assert reader.get_latest() is None  # instance-Default (1.0s) greift


def test_get_latest_explicit_none_disables_age_check_reproducing_old_sensor_snapshot_reader() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t", max_age_seconds=1.0)
    reader._on_message(None, None, _message(json.dumps({"a": 1})))
    reader._latest_received_at = time.monotonic() - 10_000.0

    assert reader.get_latest(max_age_seconds=None) == {"a": 1}


def test_get_latest_explicit_float_overrides_instance_max_age_seconds() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t", max_age_seconds=100.0)
    reader._on_message(None, None, _message(json.dumps({"a": 1})))
    reader._latest_received_at = time.monotonic() - 5.0

    assert reader.get_latest(max_age_seconds=1.0) is None  # engere Grenze übersteuert 100.0
    assert reader.get_latest(max_age_seconds=10.0) == {"a": 1}


def test_get_latest_returns_none_without_any_message_regardless_of_max_age() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t")
    assert reader.get_latest() is None
    assert reader.get_latest(max_age_seconds=None) is None
    assert reader.get_latest(max_age_seconds=100.0) is None


def test_close_never_raises_even_if_disconnect_fails() -> None:
    reader = MqttTopicReader(host="localhost", port=1883, topic="t")
    reader.client.loop_stop = lambda: None

    def _boom():
        raise OSError("already closed")

    reader.client.disconnect = _boom

    reader.close()  # darf NICHT raisen
