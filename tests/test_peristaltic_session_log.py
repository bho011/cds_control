"""
services/peristaltic/session_log.py: CSV-/Rohlog-/JSON-Ausgabe,
Dateinamens-Kollisionsvermeidung (bestehende Logs werden nie
überschrieben) und Thread-Sicherheit (TX aus dem Hauptthread, RX aus dem
Lesethread schreiben in dieselbe Rohlogdatei).
"""

from __future__ import annotations

import csv
import json
import threading
from datetime import datetime

import pytest

import services.peristaltic.session_log as session_log_module
from services.peristaltic.session_log import (
    CSV_FIELDNAMES,
    PeristalticSessionLogger,
    _resolve_unique_base_name,
)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 7, 21, 10, 0, 0, tzinfo=tz)


def test_creates_dated_subdirectory(tmp_path):
    logger = PeristalticSessionLogger("test", log_dir=tmp_path)
    try:
        assert logger.csv_path.parent.parent == tmp_path
        assert logger.csv_path.parent.name.isdigit()
        assert len(logger.csv_path.parent.name) == 8  # YYYYMMDD
    finally:
        logger.close()


def test_creates_csv_raw_and_optional_json_files(tmp_path):
    logger = PeristalticSessionLogger("test", log_dir=tmp_path)
    logger.log_raw("TX", "PING")
    logger.close({"result": "OK"})

    assert logger.csv_path.exists()
    assert logger.raw_log_path.exists()
    assert logger.json_path.exists()


def test_log_row_writes_all_expected_columns(tmp_path):
    logger = PeristalticSessionLogger("test", log_dir=tmp_path)
    logger.log_row(
        controller="MCU_B",
        controller_role="EC",
        pump="P1",
        pump_role="nutrient_a_1",
        command="DOSE",
        requested_ml=5.0,
        measured_ml=4.8,
        result="DONE",
    )
    logger.close()

    with logger.csv_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == list(CSV_FIELDNAMES)
        rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["controller"] == "MCU_B"
    assert rows[0]["session_id"] == logger.session_id
    assert rows[0]["raw_log_file"] == logger.raw_log_path.name
    assert rows[0]["requested_ml"] == "5.0"


def test_log_row_is_flushed_immediately_without_close(tmp_path):
    logger = PeristalticSessionLogger("test", log_dir=tmp_path)
    logger.log_row(controller="MCU_A", command="PING", result="OK")
    content = logger.csv_path.read_text(encoding="utf-8")
    assert "PING" in content
    logger.close()


def test_close_writes_json_summary_once(tmp_path):
    logger = PeristalticSessionLogger("test", log_dir=tmp_path)
    logger.close({"command": "test", "result": "OK"})
    summary = json.loads(logger.json_path.read_text(encoding="utf-8"))
    assert summary["result"] == "OK"


def test_close_without_summary_does_not_create_json(tmp_path):
    logger = PeristalticSessionLogger("test", log_dir=tmp_path)
    logger.close()
    assert not logger.json_path.exists()


def test_resolve_unique_base_name_avoids_existing_files(tmp_path):
    (tmp_path / "test_20260721T100000.csv").touch()
    resolved = _resolve_unique_base_name(tmp_path, "test_20260721T100000")
    assert resolved != "test_20260721T100000"
    assert not (tmp_path / f"{resolved}.csv").exists()
    assert not (tmp_path / f"{resolved}.log").exists()
    assert not (tmp_path / f"{resolved}.json").exists()


def test_two_loggers_with_same_command_in_same_second_never_overwrite_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(session_log_module, "datetime", _FixedDatetime)

    logger1 = PeristalticSessionLogger("test", log_dir=tmp_path)
    logger1.log_row(controller="MCU_B", command="PING", result="FIRST_SESSION")
    logger1.close()

    logger2 = PeristalticSessionLogger("test", log_dir=tmp_path)
    logger2.log_row(controller="MCU_B", command="PING", result="SECOND_SESSION")
    logger2.close()

    assert logger1.csv_path != logger2.csv_path
    assert logger1.csv_path.exists()
    assert logger2.csv_path.exists()

    first_content = logger1.csv_path.read_text(encoding="utf-8")
    assert "FIRST_SESSION" in first_content
    assert "SECOND_SESSION" not in first_content


def test_concurrent_tx_rx_log_raw_from_two_threads_does_not_interleave_lines(tmp_path):
    logger = PeristalticSessionLogger("test", log_dir=tmp_path)

    def writer(direction: str) -> None:
        for i in range(200):
            logger.log_raw(direction, f"{direction}-{i}")

    thread_tx = threading.Thread(target=writer, args=("TX",))
    thread_rx = threading.Thread(target=writer, args=("RX",))
    thread_tx.start()
    thread_rx.start()
    thread_tx.join()
    thread_rx.join()
    logger.close()

    lines = logger.raw_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    for line in lines:
        parts = line.split(" ", 2)
        assert len(parts) == 3
        assert parts[1] in ("TX", "RX")
