import time

from gpio_config import OUTPUTS, ACTIVE_LOW
from hardware.actuator_manager import ActuatorManager


REQUIRED_CONFIRMATION = "DRAIN_PATH_CONFIRMED"
VALVE_SETTLE_SECONDS = 1.0
DEFAULT_PULSE_SECONDS = 60.0
MAX_PULSE_SECONDS = 300.0


def ask_seconds() -> float | None:
    print()
    print("Drain-Puls wählen:")
    print(f"- Enter = {DEFAULT_PULSE_SECONDS:.0f} Sekunden")
    print(f"- Zahl  = eigene Sekunden, max. {MAX_PULSE_SECONDS:.0f}")
    print("- q     = beenden")

    value = input("Drain-Zeit: ").strip().lower()

    if value == "q":
        return None

    if value == "":
        return DEFAULT_PULSE_SECONDS

    try:
        seconds = float(value)
    except ValueError:
        print("[WARN] Ungültige Eingabe.")
        return 0.0

    if seconds <= 0:
        print("[WARN] Zeit muss größer als 0 sein.")
        return 0.0

    if seconds > MAX_PULSE_SECONDS:
        print(f"[WARN] Maximal {MAX_PULSE_SECONDS:.0f}s erlaubt. Setze auf Maximum.")
        seconds = MAX_PULSE_SECONDS

    return seconds


def run_pulse(transfer_pump, drain_valve, seconds: float):
    print()
    print(f"[DRAIN] Starte Drain für {seconds:.1f}s")
    print("[ON ] drain_valve_0")
    drain_valve.on()

    time.sleep(VALVE_SETTLE_SECONDS)

    print("[ON ] transfer_pump")
    transfer_pump.on()

    start = time.monotonic()

    try:
        while True:
            elapsed = time.monotonic() - start
            remaining = seconds - elapsed

            if remaining <= 0:
                break

            print(f"[DRAIN] läuft... Restzeit {remaining:.1f}s", end="\r")
            time.sleep(0.5)

        print()

    finally:
        print("[OFF] transfer_pump")
        transfer_pump.off()

        print("[OFF] drain_valve_0")
        drain_valve.off()

        print("[SAFE] Drain-Puls beendet. Alles AUS.")


def main():
    print("CDS Safe Drain")
    print("==============")
    print()
    print("Dieses Skript dient nur zum kontrollierten Auspumpen.")
    print("Keine Sensorlogik. Keine Automatik. Kein Rezept.")
    print()
    print("Ablauf:")
    print("1. Valve_0_Drain öffnen")
    print("2. Transferpumpe starten")
    print("3. gewählte Zeit laufen lassen")
    print("4. Transferpumpe AUS")
    print("5. Valve_0_Drain AUS")
    print()
    print(f"Zum Start exakt eingeben: {REQUIRED_CONFIRMATION}")

    confirmation = input("Bestätigung: ").strip()

    if confirmation != REQUIRED_CONFIRMATION:
        print("[BLOCKED] Sicherheitsbestätigung falsch. Abbruch.")
        return

    actuators = ActuatorManager(active_low=ACTIVE_LOW)

    try:
        transfer_pump = actuators.add(
            name="transfer_pump",
            gpio_pin=OUTPUTS["transfer_pump"],
        )

        drain_valve = actuators.add(
            name="drain_valve_0",
            gpio_pin=OUTPUTS["valve_0_drain"],
        )

        print()
        print("[READY] Safe Drain bereit.")

        while True:
            seconds = ask_seconds()

            if seconds is None:
                print("[END] Beendet.")
                break

            if seconds <= 0:
                continue

            confirm = input(f"{seconds:.1f}s drainen? ja/nein: ").strip().lower()

            if confirm != "ja":
                print("[INFO] Drain-Puls nicht gestartet.")
                continue

            run_pulse(transfer_pump, drain_valve, seconds)

    except KeyboardInterrupt:
        print("\n[ABORT] Abbruch durch Benutzer.")

    finally:
        print("[SAFE] Schalte alle Aktoren AUS.")
        actuators.safe_shutdown_all()
        actuators.close_all()
        print("[END] Safe Drain beendet.")


if __name__ == "__main__":
    main()
