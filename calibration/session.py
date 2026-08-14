"""Interaktiver Hauptablauf einer Mixing-Tank-Kalibrier-Session.

run_calibration() bleibt bewusst eine einzelne, sequenzielle Funktion, die
die physischen Prozessschritte 1:1 abbildet (Nullpunkt -> manuelles Fill ->
Pumpen-Fill -> Pumpen-Drain -> finaler Nullpunkt) - eine generische
Phasen-Abstraktion würde das schwerer nachvollziehbar machen, nicht leichter
(siehe Modularisierungs-Plan, Übergreifende Leitlinie).
"""

from __future__ import annotations

from datetime import datetime

from .analysis import analyze_measurements
from .cli_input import ask_float, ask_yes_no
from .config import (
    BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS,
    BRIDGE_MIXER_SENSOR_LITER_FACTOR,
    BRIDGE_MIXER_SENSOR_LITER_OFFSET,
    DATA_DIR,
    DEFAULT_CALIBRATION_DRAIN_MAX_SECONDS,
    DEFAULT_CALIBRATION_FILL_MAX_SECONDS,
    DEFAULT_FILL_STEP_L,
    MANUAL_FILL_TARGET_L,
    MIXER_RAW_NODE_ID,
    MIXER_SYSTEM_LITERS_NODE_ID,
    OPCUA_ENDPOINT,
    PUMP_CONTROL_ENABLED,
    PUMP_DRAIN_STEP_L,
    PUMP_FILL_FIRST_STEP_L,
    PUMP_FILL_STEP_L,
    PUMP_FILL_TARGET_L,
    SAMPLES_PER_MEASUREMENT,
    load_settings,
)
from .measurement_csv import Measurement, save_csv
from .pump_segments import (
    build_pump_drain_checkpoints,
    build_pump_fill_checkpoints,
    create_calibration_pump_actuators,
    open_trace_writer,
    run_drain_pump_segment,
    run_fill_pump_segment,
    shutdown_actuators,
    trace_csv_path,
)
from .sensor_reading import capture_measurement


async def run_calibration() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = DATA_DIR / f"mixing_tank_calibration_{timestamp}.csv"
    trace_path = trace_csv_path(csv_path)

    settings = load_settings()
    measurements: list[Measurement] = []
    current_reference_volume_l = 0.0
    fill_step = 0
    drain_step = 0
    actuators = None
    pump_control_active = False
    trace_file = None
    trace_writer = None

    print("============================================================")
    print("Mixing-Tank Sensor-Kalibrierung")
    print("============================================================")
    print(f"OPC-UA Endpoint:      {OPCUA_ENDPOINT}")
    print(f"Raw Node:             {MIXER_RAW_NODE_ID}")
    print(f"System Liter Node:    {MIXER_SYSTEM_LITERS_NODE_ID}")
    print(f"Manuelles Fill bis:   {MANUAL_FILL_TARGET_L:.1f} L")
    print(
        f"Pumpen-Fill bis:      {PUMP_FILL_TARGET_L:.1f} L "
        f"(1x {PUMP_FILL_FIRST_STEP_L:.0f}L, dann {PUMP_FILL_STEP_L:.0f}L-Schritte)"
    )
    print(f"Samples/Messpunkt:    {SAMPLES_PER_MEASUREMENT}")
    print(f"CSV-Datei:            {csv_path}")
    print(f"Trace-CSV-Datei:      {trace_path}")
    print()
    print("Bridge-Dokumentation:")
    print(f"  factor:             {BRIDGE_MIXER_SENSOR_LITER_FACTOR}")
    print(f"  offset:             {BRIDGE_MIXER_SENSOR_LITER_OFFSET}")
    print(f"  status:             {BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS}")
    print("============================================================")
    print()
    print("Ablauf:")
    print("  1. Tank leer starten, Nullpunkt erfassen.")
    print(f"  2. Manuell Wasser zufügen bis {MANUAL_FILL_TARGET_L:.0f} L (Eimer/Kanne).")
    print(f"  3. Ab {MANUAL_FILL_TARGET_L:.0f} L füllt die Mixer-Refill-Pumpe automatisch,")
    print(f"     in Markierungs-Schritten bis {PUMP_FILL_TARGET_L:.0f} L. Enter stoppt die Pumpe,")
    print("     sobald die Markierung erreicht ist - jedes Segment loggt den Rohwert")
    print("     sekündlich in die Trace-CSV.")
    print(f"  4. Danach entleert dieselbe Pumpen-/Ventilkombination in {PUMP_DRAIN_STEP_L:.0f}L-")
    print("     Schritten wieder bis 0 L, ebenfalls mit sekündlichem Trace-Log.")
    print("  5. Finalen Nullpunkt nach Drain erfassen.")
    print()
    print("Wichtig:")
    print("  Kalibriereinstellungen NICHT mehr ändern.")
    print("  Bewegte/gestörte Messpunkte als ungültig markieren.")
    print()

    if not ask_yes_no("Kalibrierung starten?", default=True):
        print("Abgebrochen.")
        return

    try:
        if PUMP_CONTROL_ENABLED and ask_yes_no(
            f"Pumpengesteuerte Fill-/Drain-Segmente (ab {MANUAL_FILL_TARGET_L:.0f} L) durchführen?",
            default=True,
        ):
            try:
                actuators = create_calibration_pump_actuators()
                trace_file, trace_writer = open_trace_writer(trace_path)
                pump_control_active = True
                print("[OK] Fill-/Drain-Aktoren initialisiert.")
            except Exception as exc:
                print(f"[ERROR] Aktoren/Trace-Datei konnten nicht initialisiert werden: {exc}")
                print("[INFO] Pumpengesteuerte Segmente werden übersprungen.")
                pump_control_active = False

        # ----------------------------------------------------
        # 0-Liter-Punkt
        # ----------------------------------------------------
        print("\n------------------------------------------------------------")
        print("NULLPUNKT")
        print("------------------------------------------------------------")
        input("Tank vollständig leeren. Danach Enter drücken, um 0 L zu erfassen...")

        zero_measurement = await capture_measurement(
            phase="zero",
            step=0,
            manual_added_l=0.0,
            manual_drained_l=0.0,
            reference_volume_l=0.0,
            note="empty tank zero point",
        )

        measurements.append(zero_measurement)
        save_csv(csv_path, measurements)
        print(f"CSV aktualisiert: {csv_path}")

        # ----------------------------------------------------
        # MANUELLE FILL-PHASE (0 -> MANUAL_FILL_TARGET_L)
        # ----------------------------------------------------
        print("\n------------------------------------------------------------")
        print(f"MANUELLE FILL-PHASE (bis {MANUAL_FILL_TARGET_L:.0f} L)")
        print("------------------------------------------------------------")

        while current_reference_volume_l < MANUAL_FILL_TARGET_L:
            print(f"\nAktuelles Referenzvolumen: {current_reference_volume_l:.3f} L")
            print(f"Ziel: {MANUAL_FILL_TARGET_L:.3f} L")

            if not ask_yes_no("Weiteren Fill-Messpunkt erfassen?", default=True):
                break

            print()
            print("Bitte Wasser manuell in den Tank füllen.")
            added_l = ask_float("Wie viele Liter wurden hinzugefügt?", default=DEFAULT_FILL_STEP_L)

            if added_l <= 0:
                print("Wert muss größer als 0 sein.")
                continue

            current_reference_volume_l += added_l
            fill_step += 1

            measurement = await capture_measurement(
                phase="fill",
                step=fill_step,
                manual_added_l=added_l,
                manual_drained_l=0.0,
                reference_volume_l=current_reference_volume_l,
                note="manual fill",
            )

            measurements.append(measurement)
            save_csv(csv_path, measurements)
            print(f"CSV aktualisiert: {csv_path}")

            if current_reference_volume_l >= MANUAL_FILL_TARGET_L:
                print(f"\nManuelles Zielvolumen erreicht: {current_reference_volume_l:.3f} L")
                break

        # ----------------------------------------------------
        # PUMPEN-FILL-PHASE (MANUAL_FILL_TARGET_L -> PUMP_FILL_TARGET_L)
        # ----------------------------------------------------
        if pump_control_active and current_reference_volume_l < PUMP_FILL_TARGET_L:
            print("\n------------------------------------------------------------")
            print(f"PUMPEN-FILL-PHASE (bis {PUMP_FILL_TARGET_L:.0f} L)")
            print("------------------------------------------------------------")

            fill_checkpoints = build_pump_fill_checkpoints(
                current_reference_volume_l, PUMP_FILL_FIRST_STEP_L, PUMP_FILL_STEP_L, PUMP_FILL_TARGET_L
            )
            max_fill_segment_seconds = float(
                settings.get("calibration_fill_max_seconds", DEFAULT_CALIBRATION_FILL_MAX_SECONDS)
            )
            refill_pump = actuators.get("mixer_refill_pump")

            for checkpoint in fill_checkpoints:
                print(f"\nAktuelles Referenzvolumen: {current_reference_volume_l:.3f} L")
                print(f"Nächste Markierung: {checkpoint:.1f} L")

                if not ask_yes_no("Pump-Fill-Segment starten?", default=True):
                    break

                fill_step += 1

                await run_fill_pump_segment(
                    refill_pump=refill_pump,
                    max_segment_seconds=max_fill_segment_seconds,
                    segment_index=fill_step,
                    segment_target_liters=checkpoint,
                    trace_writer=trace_writer,
                    trace_file=trace_file,
                )

                confirmed_l = ask_float(
                    "Welchen Literwert zeigt die Markierung am Tank gerade an?",
                    default=checkpoint,
                )
                current_reference_volume_l = confirmed_l

                measurement = await capture_measurement(
                    phase="fill",
                    step=fill_step,
                    manual_added_l=0.0,
                    manual_drained_l=0.0,
                    reference_volume_l=current_reference_volume_l,
                    note=f"pump fill checkpoint, target={checkpoint:.1f}L",
                )

                measurements.append(measurement)
                save_csv(csv_path, measurements)
                print(f"CSV aktualisiert: {csv_path}")

                if current_reference_volume_l >= PUMP_FILL_TARGET_L:
                    print(f"\nPumpen-Zielvolumen erreicht: {current_reference_volume_l:.3f} L")
                    break

        # ----------------------------------------------------
        # PUMPEN-DRAIN-PHASE (aktuelles Volumen -> 0 L)
        # ----------------------------------------------------
        if pump_control_active and current_reference_volume_l > 0:
            print("\n------------------------------------------------------------")
            print("PUMPEN-DRAIN-PHASE (bis 0 L)")
            print("------------------------------------------------------------")

            drain_checkpoints = build_pump_drain_checkpoints(current_reference_volume_l, PUMP_DRAIN_STEP_L)
            max_drain_segment_seconds = float(
                settings.get("calibration_drain_max_seconds", DEFAULT_CALIBRATION_DRAIN_MAX_SECONDS)
            )
            valve_settle_seconds = float(settings.get("valve_settle_seconds", 1.0))
            transfer_pump = actuators.get("transfer_pump")
            drain_valve = actuators.get("drain_valve_0")

            for checkpoint in drain_checkpoints:
                print(f"\nAktuelles Referenzvolumen: {current_reference_volume_l:.3f} L")
                print(f"Nächste Markierung: {checkpoint:.1f} L")

                if not ask_yes_no("Pump-Drain-Segment starten?", default=True):
                    break

                drain_step += 1

                await run_drain_pump_segment(
                    transfer_pump=transfer_pump,
                    drain_valve=drain_valve,
                    max_segment_seconds=max_drain_segment_seconds,
                    valve_settle_seconds=valve_settle_seconds,
                    segment_index=drain_step,
                    segment_target_liters=checkpoint,
                    trace_writer=trace_writer,
                    trace_file=trace_file,
                )

                confirmed_l = ask_float(
                    "Welchen Literwert zeigt die Markierung am Tank gerade an?",
                    default=checkpoint,
                )
                current_reference_volume_l = confirmed_l

                measurement = await capture_measurement(
                    phase="drain",
                    step=drain_step,
                    manual_added_l=0.0,
                    manual_drained_l=0.0,
                    reference_volume_l=current_reference_volume_l,
                    note=f"pump drain checkpoint, target={checkpoint:.1f}L",
                )

                measurements.append(measurement)
                save_csv(csv_path, measurements)
                print(f"CSV aktualisiert: {csv_path}")

                if current_reference_volume_l <= 0:
                    print("\nTank rechnerisch leer.")
                    break

        # ----------------------------------------------------
        # FINALER NULLPUNKT
        # ----------------------------------------------------
        print("\n------------------------------------------------------------")
        print("FINALER NULLPUNKT")
        print("------------------------------------------------------------")

        if ask_yes_no("Finalen 0-L-Punkt nach Drain erfassen?", default=True):
            input("Tank so gut wie möglich leeren. Danach Enter drücken...")

            final_zero = await capture_measurement(
                phase="zero_after_drain",
                step=0,
                manual_added_l=0.0,
                manual_drained_l=0.0,
                reference_volume_l=0.0,
                note="empty tank after drain",
            )

            measurements.append(final_zero)
            save_csv(csv_path, measurements)
            print(f"CSV aktualisiert: {csv_path}")

    finally:
        if actuators is not None:
            print("[SAFE] Shutdown all calibration actuators.")
            shutdown_actuators(actuators)

        if trace_file is not None:
            trace_file.close()

    analyze_measurements(measurements)

    print("\n============================================================")
    print("KALIBRIERUNG BEENDET")
    print("============================================================")
    print("CSV-Datei gespeichert unter:")
    print(f"  {csv_path}")
    if trace_writer is not None:
        print("Trace-CSV (sekündliche Rohwerte während Pump-Segmenten) unter:")
        print(f"  {trace_path}")
    print()
    print("Diese CSV bitte aufheben. Daraus können wir später")
    print("eine Kalibriertabelle, Interpolation oder finale Sensorformel ableiten.")
