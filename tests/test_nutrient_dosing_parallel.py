"""
AP-05: rundenbasierte parallele MCU_B-Dosierung (process/nutrient_dosing.py), hardwarefrei.

Der Dosierer startet je Runde alle Chunks (Runde r = r-ter Chunk jeder Pumpe, die noch Chunks hat), ohne auf
DONE zu warten, und beginnt die naechste Runde erst, wenn jede Pumpe der Runde mit DONE + IDLE bestaetigt ist.
Hier steuert der TEST ueber ``ManualCompletionMcu`` selbst, welche Pumpe wann fertig wird: so ist die
tatsaechliche Ueberlappung (gleichzeitig BUSY) belegbar, ohne Zeit-Flakiness. Kein Port, kein GPIO.
"""

from __future__ import annotations

import threading
import time

import pytest
from fake_mcu import AutoCompletingFakeMcu, FakeSerialPort, ManualCompletionMcu, wait_until
from test_nutrient_dosing import FW_ML_PER_STEP, SETTINGS, _controller, _doses, _first_round_ul, _plan, _setup, _wait

from domain.recipe.nutrient_plan import DoseChunk, NutrientDosingPlan
from process.nutrient_dosing import NUTRIENT_MAX_DOSE_ML, DosingPhase, NutrientDosingController

PUMPS = ["P1", "P2", "P3", "P4"]


def _start(tmp_path, monkeypatch, plan=None, mcu=None):
    """Dosierlauf mit steuerbarem MCU starten. Rueckgabe: (plan, mcu, port, controller)."""
    _setup(monkeypatch, tmp_path)
    plan = plan or _plan()
    mcu = mcu or ManualCompletionMcu()
    port = FakeSerialPort(mcu)
    mcu.attach(port)
    controller = _controller(tmp_path, port)
    assert controller.start(SETTINGS, plan)["success"]
    return plan, mcu, port, controller


def _chunk(plan, pump: str, index: int = 0) -> int:
    return plan.pump_target(pump).chunks_ul[index]


def _drive(mcu, controller, timeout: float = 15.0) -> None:
    """Beendet jede laufende Dosis in umgekehrter Pumpenreihenfolge, bis der Dosierer fertig ist."""
    deadline = time.monotonic() + timeout
    while controller.is_active() and time.monotonic() < deadline:
        for pump in reversed(mcu.busy_pumps()):
            mcu.finish(pump)
        time.sleep(0.005)
    assert not controller.is_active(), "Dosierer wurde nicht rechtzeitig fertig"


# --- tatsaechliche Ueberlappung und Rundengrenzen ------------------------------------------------


def test_all_four_pumps_run_together_and_the_next_round_waits_for_the_slowest(tmp_path, monkeypatch):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)

    # Alle vier Pumpen der Runde sind gestartet, BEVOR irgendeine fertig ist: gleichzeitig BUSY.
    assert wait_until(lambda: len(mcu.started()) == 4)
    assert mcu.started() == PUMPS
    assert mcu.max_busy == 4 and mcu.busy_pumps() == PUMPS
    assert not any(kind == "DONE" for kind, _ in mcu.events)
    status = controller.get_status()
    assert status["active_pumps"] == PUMPS
    assert status["in_flight_ul"] == _first_round_ul(plan) and status["confirmed_ul"] == 0

    # Drei Pumpen sind fertig, die vierte laeuft noch: keine neue Runde, auch nicht fuer die fertigen Pumpen.
    for pump in ("P1", "P2", "P3"):
        mcu.finish(pump)
    done_ul = sum(_chunk(plan, pump) for pump in ("P1", "P2", "P3"))
    assert wait_until(lambda: controller.get_status()["confirmed_ul"] == done_ul)
    time.sleep(0.4)
    assert len(mcu.started()) == 4 and mcu.busy_pumps() == ["P4"]

    # Erst wenn auch die letzte fertig ist, beginnt Runde 2 - wieder mit allen vier Pumpen.
    mcu.finish("P4")
    assert wait_until(lambda: len(mcu.started()) == 8)
    assert mcu.started()[4:] == PUMPS
    _drive(mcu, controller)

    status = controller.get_status()
    assert status["phase"] == DosingPhase.FINISHED and status["completion_reason"] == "completed"
    assert status["confirmed_ul"] == plan.total_ul and status["uncertain_ul"] == 0 and status["in_flight_ul"] == 0


def test_different_completion_order_is_booked_per_pump(tmp_path, monkeypatch):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)

    finished: list[str] = []
    for pump in ("P3", "P1", "P4", "P2"):  # bewusst weder Pumpenreihenfolge noch paarweise
        mcu.finish(pump)
        finished.append(pump)
        expected = sum(_chunk(plan, p) for p in finished)
        assert wait_until(lambda expected=expected: controller.get_status()["confirmed_ul"] == expected)
        if len(finished) < len(PUMPS):
            # Solange eine Pumpe der Runde laeuft, sind genau deren Chunks noch in Ausfuehrung.
            status = controller.get_status()
            assert status["in_flight_ul"] == _first_round_ul(plan) - expected
            assert status["active_pumps"] == [p for p in PUMPS if p not in finished]
    # Mit der letzten Pumpe ist Runde 1 komplett bestaetigt; Runde 2 startet sofort mit allen vier.
    assert wait_until(lambda: len(mcu.started()) == 8)
    _drive(mcu, controller)
    assert controller.get_status()["confirmed_ul"] == plan.total_ul


def test_unequal_chunk_counts_shrink_the_rounds_without_restarting_finished_pumps(tmp_path, monkeypatch):
    plan = _plan(a_percent=80.0)  # A 16 ml: P1/P3 je 2 Chunks; B 4 ml: P2/P4 je 1 Chunk
    assert [plan.pump_target(p).chunk_count for p in PUMPS] == [2, 1, 2, 1]
    plan, mcu, port, controller = _start(tmp_path, monkeypatch, plan)

    assert wait_until(lambda: len(mcu.started()) == 4)
    for pump in PUMPS:
        mcu.finish(pump)
    assert wait_until(lambda: len(mcu.started()) == 6)
    assert mcu.started() == PUMPS + ["P1", "P3"]  # Runde 2: nur noch Pumpen mit offenen Auftraegen
    assert wait_until(lambda: mcu.busy_pumps() == ["P1", "P3"])
    _drive(mcu, controller)

    assert mcu.started() == PUMPS + ["P1", "P3"]  # fertige Pumpen (P2, P4) wurden nie erneut gestartet
    status = controller.get_status()
    assert status["confirmed_ul"] == plan.total_ul and status["uncertain_ul"] == 0
    for pump_status in status["pumps"]:
        assert pump_status["confirmed_ul"] == pump_status["target_ul"]
        assert pump_status["chunks_done"] == pump_status["chunks_total"]


def test_remaining_pumps_may_be_a_subset_that_is_not_an_allowed_group_by_itself(tmp_path, monkeypatch):
    # P1 hat dreimal so viel Gewicht wie P3 (P1 2 Chunks, P3 1 Chunk): Runde 2 ist {P1, P2, P4}. Das ist keine
    # eigene erlaubte Gruppe, aber der Rueckbau der gestarteten Gruppe (alle vier) - es kommt keine Pumpe hinzu.
    weights = {"P1": 3 * FW_ML_PER_STEP, "P2": FW_ML_PER_STEP, "P3": FW_ML_PER_STEP, "P4": FW_ML_PER_STEP}
    plan = _plan(pump_weights=weights)
    assert [plan.pump_target(p).chunk_count for p in PUMPS] == [2, 2, 1, 2]
    plan, mcu, port, controller = _start(tmp_path, monkeypatch, plan)

    assert wait_until(lambda: len(mcu.started()) == 4)
    for pump in PUMPS:
        mcu.finish(pump)
    assert wait_until(lambda: len(mcu.started()) == 7)
    assert mcu.started()[4:] == ["P1", "P2", "P4"]
    _drive(mcu, controller)
    assert controller.get_status()["confirmed_ul"] == plan.total_ul


@pytest.mark.parametrize("a_percent,pair", [(100.0, ["P1", "P3"]), (0.0, ["P2", "P4"])])
def test_single_solution_run_uses_only_its_pump_pair(tmp_path, monkeypatch, a_percent, pair):
    plan = _plan(a_percent=a_percent)  # 20 ml auf eine Loesung: 10 ml je Pumpe, 3 Chunks
    plan, mcu, port, controller = _start(tmp_path, monkeypatch, plan)

    assert wait_until(lambda: len(mcu.started()) == 2)
    assert mcu.started() == pair and mcu.max_busy == 2
    _drive(mcu, controller)

    assert mcu.started() == pair * 3  # drei Runden, nie eine Pumpe der anderen Loesung
    assert controller.get_status()["confirmed_ul"] == plan.total_ul


def test_chunks_never_exceed_the_prototype_limit_and_amounts_add_up(tmp_path, monkeypatch):
    plan = _plan(ro_l=40.0, ml_per_100_l=300.0, max_chunk_ml=10.0)  # 120 ml, je Pumpe 30 ml in 3 Chunks zu 10 ml
    assert max(max(t.chunks_ul) for t in plan.pump_targets) == 10_000
    plan, mcu, port, controller = _start(tmp_path, monkeypatch, plan)
    _drive(mcu, controller)

    doses = _doses(port)
    assert len(doses) == 12 and all(float(line.split()[2]) <= NUTRIENT_MAX_DOSE_ML for line in doses)
    status = controller.get_status()
    assert status["confirmed_ul"] == plan.total_ul == sum(p["target_ul"] for p in status["pumps"])
    assert status["phase"] == DosingPhase.FINISHED


# --- Pumpengruppen (vor jeder Hardwareausgabe) ---------------------------------------------------


def test_start_group_that_is_not_allowed_is_blocked_before_any_output(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    # P1 bekommt praktisch die gesamte Loesung A, P3 null Chunks: die Startgruppe waere {P1, P2, P4}.
    weights = {"P1": 1e9 * FW_ML_PER_STEP, "P2": FW_ML_PER_STEP, "P3": FW_ML_PER_STEP, "P4": FW_ML_PER_STEP}
    plan = _plan(pump_weights=weights)
    assert plan.pump_target("P3").chunks_ul == ()

    result = _controller(tmp_path).start(SETTINGS, plan)  # der Standard-Controller wirft, sobald ein Port geoeffnet wird
    assert not result["success"] and "nicht zulässig" in result["message"] and "P1,P2,P4" in result["message"]


def test_a_follow_up_round_can_never_add_a_pump(tmp_path, monkeypatch):
    def forged_rounds(self):
        yield (DoseChunk("P1", 1, 2500), DoseChunk("P3", 1, 2500))  # erlaubte Startgruppe {P1, P3}
        yield (DoseChunk("P1", 2, 2500), DoseChunk("P2", 1, 2500), DoseChunk("P3", 2, 2500))  # P2 kommt hinzu

    monkeypatch.setattr(NutrientDosingPlan, "iter_rounds", forged_rounds)
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)

    assert wait_until(lambda: len(mcu.started()) == 2)
    mcu.finish("P1")
    mcu.finish("P3")
    _wait(controller)

    status = controller.get_status()
    assert status["phase"] == DosingPhase.ERROR and status["completion_reason"] == "safety_check_failed"
    assert "zusätzliche Pumpen" in status["last_error"]
    assert mcu.started() == ["P1", "P3"]  # die dritte Pumpe wurde nie gestartet
    assert status["confirmed_ul"] == 5000 and status["uncertain_ul"] == 0


# --- Ausfall einzelner Pumpen: alles stoppen, nichts wiederholen ------------------------------------


def test_firmware_timeout_of_one_pump_stops_all_pumps_and_marks_the_running_chunks_uncertain(tmp_path, monkeypatch):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)
    mcu.finish("P1")
    assert wait_until(lambda: controller.get_status()["confirmed_ul"] == _chunk(plan, "P1"))

    mcu.fail("P3")  # ERR P3 TIMEOUT, waehrend P2 und P4 noch laufen
    _wait(controller)

    status = controller.get_status()
    assert status["phase"] == DosingPhase.ERROR and status["completion_reason"] == "timeout"
    assert status["confirmed_ul"] == _chunk(plan, "P1")  # bleibt bestaetigt
    assert status["uncertain_ul"] == sum(_chunk(plan, p) for p in ("P2", "P3", "P4"))
    assert status["in_flight_ul"] == 0
    assert not any(mcu.busy.values()) and mcu.events[-1] == ("STOPALL", "")  # STOPALL stoppt auch P2 und P4
    assert mcu.started() == PUMPS  # keine Wiederholung, keine zweite Runde, keine Umverteilung


class _RefuseP2(ManualCompletionMcu):
    def handle_command(self, line: str) -> list[str]:
        if line.startswith("DOSE P2"):
            return ["ERR P2 BUSY"]
        return super().handle_command(line)


def test_start_refusal_of_the_second_pump_marks_only_the_started_pump_uncertain(tmp_path, monkeypatch):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch, mcu=_RefuseP2())
    _wait(controller)

    status = controller.get_status()
    assert status["phase"] == DosingPhase.ERROR
    assert mcu.started() == ["P1"] and len(_doses(port)) == 2  # P1 lief, der Befehl fuer P2 wurde abgelehnt
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] == _chunk(plan, "P1")  # P2 nie gefoerdert
    assert not any(mcu.busy.values()) and port.written_lines.count("STOPALL") >= 2


def test_done_with_wrong_amount_from_one_pump_is_an_error(tmp_path, monkeypatch):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)
    port.inject_line("DONE P2 0.500")  # P2 haette 2.500 ml melden muessen
    _wait(controller)

    status = controller.get_status()
    assert status["completion_reason"] == "safety_check_failed" and "DONE meldet" in status["last_error"]
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] == _first_round_ul(plan)
    assert not any(mcu.busy.values())


def test_a_duplicate_done_for_a_still_running_pump_is_never_confirmed(tmp_path, monkeypatch):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)
    port.inject_line("DONE P1 2.500")  # Menge stimmt, aber P1 laeuft laut STATUS noch (BUSY)
    _wait(controller)

    status = controller.get_status()
    assert status["completion_reason"] == "safety_check_failed" and "nicht IDLE" in status["last_error"]
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] == _first_round_ul(plan)


def test_connection_loss_during_a_round_keeps_confirmed_chunks_and_marks_the_rest_uncertain(tmp_path, monkeypatch):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)
    mcu.finish("P1")
    assert wait_until(lambda: controller.get_status()["confirmed_ul"] == _chunk(plan, "P1"))

    port.simulate_disconnect()
    _wait(controller)

    status = controller.get_status()
    assert status["completion_reason"] == "connection_error" and status["phase"] == DosingPhase.ERROR
    assert status["confirmed_ul"] == _chunk(plan, "P1")
    assert status["uncertain_ul"] == _first_round_ul(plan) - _chunk(plan, "P1")
    assert mcu.started() == PUMPS


# --- Abbruch und Emergency Stop ---------------------------------------------------------------------


@pytest.mark.parametrize("reason,expected", [("stopped_by_user", "user_abort"), ("emergency_stop", "emergency_stop")])
def test_stop_mid_round_keeps_confirmed_chunks_and_marks_the_rest_uncertain(tmp_path, monkeypatch, reason, expected):
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)
    mcu.finish("P1")
    assert wait_until(lambda: controller.get_status()["confirmed_ul"] == _chunk(plan, "P1"))

    t0 = time.monotonic()
    controller.request_stop(reason=reason)
    _wait(controller, timeout=3.0)

    assert time.monotonic() - t0 < 2.0
    assert not any(mcu.busy.values()) and mcu.events[-1] == ("STOPALL", "")
    status = controller.get_status()
    assert status["phase"] == DosingPhase.ABORTED and status["completion_reason"] == expected
    assert status["confirmed_ul"] == _chunk(plan, "P1")
    assert status["uncertain_ul"] == _first_round_ul(plan) - _chunk(plan, "P1") and status["in_flight_ul"] == 0
    assert mcu.started() == PUMPS


def test_stop_between_rounds_leaves_nothing_uncertain(tmp_path, monkeypatch):
    original = NutrientDosingController._dose_round

    def stop_after_the_first_round(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.request_stop("stopped_by_user")  # Runde 1 ist komplett bestaetigt, Runde 2 noch nicht gestartet

    monkeypatch.setattr(NutrientDosingController, "_dose_round", stop_after_the_first_round)
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)
    for pump in PUMPS:
        mcu.finish(pump)
    _wait(controller)

    status = controller.get_status()
    assert status["phase"] == DosingPhase.ABORTED and status["completion_reason"] == "user_abort"
    assert status["confirmed_ul"] == _first_round_ul(plan)
    assert status["uncertain_ul"] == 0 and status["in_flight_ul"] == 0
    assert mcu.started() == PUMPS  # keine zweite Runde


def test_a_failing_dosing_is_visible_as_stopping_before_the_link_is_closed(tmp_path, monkeypatch):
    """Die State Machine schaltet die RO-Pumpe bei jeder Phase ausser RUNNING/FINISHED ab. Der Abbruch muss also
    sofort (vor STOPALL und close(), die bei totem Link Sekunden dauern koennen) als STOPPING sichtbar sein."""
    plan, mcu, port, controller = _start(tmp_path, monkeypatch)
    assert wait_until(lambda: len(mcu.started()) == 4)
    seen_phases: list[str] = []
    original_stop_all = mcu.handle_command

    def spy(line: str) -> list[str]:
        if line.strip() == "STOPALL":
            seen_phases.append(controller.get_status()["phase"])
        return original_stop_all(line)

    mcu.handle_command = spy  # type: ignore[method-assign]
    mcu.fail("P2")
    _wait(controller)

    assert seen_phases and seen_phases[0] == DosingPhase.STOPPING  # schon beim ersten STOPALL
    assert controller.get_status()["phase"] == DosingPhase.ERROR


# --- ein Thread, konsistenter Status -----------------------------------------------------------------


def test_exactly_one_thread_operates_the_serial_client(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    port = FakeSerialPort(AutoCompletingFakeMcu())
    controller = _controller(tmp_path, port)
    assert controller.start(SETTINGS, _plan())["success"]
    _wait(controller)

    assert controller.get_status()["phase"] == DosingPhase.FINISHED
    assert len(port.write_thread_ids) == 1  # nur der Dosier-Thread - kein Thread je Pumpe
    assert threading.get_ident() not in port.write_thread_ids


def test_status_stays_consistent_while_pumps_overlap(tmp_path, monkeypatch):
    plan = _plan(ro_l=40.0, ml_per_100_l=300.0, max_chunk_ml=10.0)
    plan, mcu, port, controller = _start(tmp_path, monkeypatch, plan)

    snapshots: list[dict] = []
    stop_reader = threading.Event()

    def read_status_continuously():
        while not stop_reader.is_set() and len(snapshots) < 200_000:
            snapshots.append(controller.get_status())
            time.sleep(0.0002)  # dicht genug, um jeden Chunk-Wechsel zu treffen, ohne den Lauf auszubremsen

    reader = threading.Thread(target=read_status_continuously, daemon=True)
    reader.start()
    try:
        _drive(mcu, controller)
    finally:
        stop_reader.set()
        reader.join(timeout=5)
    snapshots.append(controller.get_status())  # Endstand nach dem Lauf

    assert len(snapshots) > 50
    last_confirmed = 0
    for snap in snapshots:
        # confirmed + in_flight + uncertain wird NIE groesser als der Plan, confirmed faellt NIE zurueck.
        assert snap["confirmed_ul"] + snap["in_flight_ul"] + snap["uncertain_ul"] <= snap["target_ul"]
        assert snap["confirmed_ul"] >= last_confirmed
        last_confirmed = snap["confirmed_ul"]
        assert snap["in_flight_ul"] <= 4 * 10_000  # hoechstens ein Chunk je Pumpe
        assert snap["confirmed_ul"] == sum(p["confirmed_ul"] for p in snap["pumps"])
        assert snap["in_flight_ul"] == sum(p["in_flight_ul"] for p in snap["pumps"])
        for p in snap["pumps"]:
            assert p["confirmed_ul"] + p["in_flight_ul"] + p["uncertain_ul"] <= p["target_ul"]
    assert snapshots[-1]["confirmed_ul"] == plan.total_ul
