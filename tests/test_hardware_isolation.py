"""
AP-00 Regressionstests fuer die zentrale Hardware-Isolation
(tests/_hardware_guard.py, installiert durch tests/conftest.py).

Alle Nachweise laufen ausschliesslich gegen die Blocker bzw. Fakes; kein Test
darf reale GPIO-Pins, Serial-Ports oder die produktive Sperrdatei beruehren.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _hardware_guard
from _hardware_guard import HardwareAccessBlocked

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_LOCK = REPO_ROOT / "logs" / ".hardware.lock"

FILL_SETTINGS = {
    "fill_mode": "delta",
    "target_add_liters": 1.0,
    "target_total_liters": 1.0,
    "max_mixer_liters": 185.0,
    "min_ro_liters_required": 20.0,
    "max_fill_seconds": 20.0,
    "enable_mixing_circulation": False,
    "enable_sensor_circulation": False,
    "hardware_execution_enabled": True,
    "required_confirmation_text": "confirmed",
}


def test_guard_is_active_before_collection():
    import gpiozero

    assert os.environ.get("CDS_TEST_HARDWARE_GUARD") == "1"
    assert isinstance(gpiozero.Device.pin_factory, _hardware_guard._BlockingPinFactory)


# --- Test A: vergessener Mock / GPIO -----------------------------------------

def test_digital_output_is_blocked(expect_blocked_access):
    from hardware.digital_output import DigitalOutput

    with pytest.raises(HardwareAccessBlocked):
        DigitalOutput(name="mixer_refill_pump", gpio_pin=17, active_low=True)
    assert expect_blocked_access


def test_actuator_manager_add_is_blocked_and_registers_nothing(expect_blocked_access):
    from hardware.actuator_manager import ActuatorManager

    manager = ActuatorManager(active_low=True)
    try:
        with pytest.raises(HardwareAccessBlocked):
            manager.add("mixer_refill_pump", 17)
        assert manager.status_payload() == {}
    finally:
        manager.close_all()


def test_lgpio_entry_points_are_blocked(expect_blocked_access):
    lgpio = pytest.importorskip("lgpio")
    with pytest.raises(HardwareAccessBlocked):
        lgpio.gpiochip_open(0)


def test_real_fill_run_without_stub_never_reaches_gpio(controller, expect_blocked_access):
    """Der Vorfall vom 14.08.: ein veralteter _run-Stub legte den echten Pfad
    frei. Hier ist KEIN Stub installiert, hardware_execution_enabled=True."""
    controller.fill_and_measure._run(dict(FILL_SETTINGS))

    assert any("gpiozero" in a for a in expect_blocked_access)
    assert controller.fill_and_measure._actuators.status_payload() == {}
    assert "Process failed" in (controller.fill_and_measure._last_error or "")


def test_swallowed_block_is_still_recorded(expect_blocked_access):
    """Faengt Produktivcode die Exception (except Exception), bleibt der
    Versuch in BLOCKED_ATTEMPTS - die autouse-Fixture wuerde den Test sonst
    scheitern lassen."""
    from hardware.digital_output import DigitalOutput

    try:
        DigitalOutput(name="x", gpio_pin=5, active_low=True)
    except Exception:
        pass
    assert len(expect_blocked_access) >= 1


# --- Test B: Serial -----------------------------------------------------------

@pytest.mark.parametrize("port", ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/serial/by-path/pci-0:1"])
def test_default_serial_factory_is_blocked_before_open(port, expect_blocked_access):
    from services.peristaltic.serial_client import default_serial_factory

    with pytest.raises(HardwareAccessBlocked):
        default_serial_factory(port, 115200, 1.0)
    assert any("serial" in a for a in expect_blocked_access)


def test_raw_device_open_is_blocked(expect_blocked_access):
    with pytest.raises(HardwareAccessBlocked):
        open("/dev/ttyUSB0", "rb")
    with pytest.raises(HardwareAccessBlocked):
        os.open("/dev/ttyACM0", os.O_RDWR)
    assert len(expect_blocked_access) == 2


# --- Test D: Fake-MCUs funktionieren weiterhin ---------------------------------

def test_fake_serial_port_still_works():
    from fake_mcu import FakeSerialPort, make_client_factory

    port = FakeSerialPort()
    assert make_client_factory(port)("/dev/ttyUSB0", 115200, 1.0) is port  # Pfad wird nie geoeffnet


# --- Produktivdaten / Netz -----------------------------------------------------

@pytest.mark.parametrize("name", ["logs/ap00_probe.txt", "config/ap00_probe.json", "recipes/ap00_probe.json",
                                  "calibration_data/ap00_probe.json"])
def test_writes_to_production_dirs_are_blocked(name, expect_blocked_access):
    target = REPO_ROOT / name
    with pytest.raises(HardwareAccessBlocked):
        target.write_text("x")
    assert not target.exists()


def test_production_lock_path_cannot_be_opened_for_writing(expect_blocked_access):
    with pytest.raises(HardwareAccessBlocked):
        PRODUCTION_LOCK.open("w")


def test_reading_production_config_is_still_allowed():
    assert (REPO_ROOT / "gpio_config.py").read_text()


def test_network_connect_is_blocked(expect_blocked_access):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(HardwareAccessBlocked):
            s.connect(("127.0.0.1", 1883))


# --- Test C: Lock -------------------------------------------------------------

def test_actuator_manager_lock_path_is_not_production():
    import hardware.actuator_manager as am

    assert am.LOCK_PATH != PRODUCTION_LOCK
    assert PRODUCTION_LOCK not in am.LOCK_PATH.parents
    assert Path(os.environ[_hardware_guard.ENV_LOCK_PATH]) == am.LOCK_PATH


# --- Test E: Subprozess-Isolation ----------------------------------------------

_CHILD = r"""
import json, os, sys
sys.path.insert(0, ".")
results = {}
from hardware.actuator_manager import ActuatorManager, LOCK_PATH
results["lock"] = str(LOCK_PATH)
from hardware.digital_output import DigitalOutput
for label, fn in {
    "gpio": lambda: DigitalOutput("p", 17, True),
    "serial": lambda: __import__("serial").Serial("/dev/ttyUSB0"),
    "device": lambda: open("/dev/ttyACM0", "rb"),
    "prod_write": lambda: open("logs/ap00_child_probe.txt", "w"),
}.items():
    try:
        fn()
        results[label] = "OPENED"
    except RuntimeError as exc:
        results[label] = "blocked" if "AP-00" in str(exc) else "other:" + str(exc)
    except Exception as exc:
        results[label] = "other:" + type(exc).__name__
print(json.dumps(results))
"""


def test_subprocess_inherits_isolation(tmp_path, expect_blocked_access):
    env = dict(os.environ)
    env[_hardware_guard.ENV_LOCK_PATH] = str(tmp_path / "child.lock")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    assert results["gpio"] == results["serial"] == results["device"] == results["prod_write"] == "blocked", results
    assert results["lock"] == str(tmp_path / "child.lock")
    # Die vier provozierten Blockaden muessen im Hauptprozess ankommen (und werden quittiert).
    reported = _hardware_guard.drain_child_attempts()
    assert len(reported) >= 4, reported
    assert not (REPO_ROOT / "logs" / "ap00_child_probe.txt").exists()


def test_subprocess_env_carries_guard_site():
    site = str(Path(__file__).resolve().parent / "_guard_site")
    assert site in os.environ["PYTHONPATH"].split(os.pathsep)


# --- Kindprozess-Schutz (Spawn-Guard) ----------------------------------------

def test_spawn_external_program_is_blocked(expect_blocked_access):
    with pytest.raises(HardwareAccessBlocked):
        subprocess.run(["/bin/echo", "x"], capture_output=True)
    assert expect_blocked_access


def test_spawn_shell_and_os_system_are_blocked(expect_blocked_access):
    with pytest.raises(HardwareAccessBlocked):
        subprocess.run("echo x", shell=True, capture_output=True)
    with pytest.raises(HardwareAccessBlocked):
        os.system("echo x")
    with pytest.raises(HardwareAccessBlocked):
        os.execv("/bin/echo", ["echo", "x"])
    assert len(expect_blocked_access) == 3


@pytest.mark.parametrize("flag", ["-I", "-S", "-E", "-Sc", "-IS"])
def test_spawn_python_with_guard_bypass_flags_is_blocked(flag, expect_blocked_access):
    code = "print(1)"
    argv = [sys.executable, flag] + ([] if flag.endswith("c") else ["-c"]) + [code]
    with pytest.raises(HardwareAccessBlocked):
        subprocess.run(argv, capture_output=True)
    assert expect_blocked_access


def test_spawn_python_with_cleaned_env_is_blocked(expect_blocked_access):
    with pytest.raises(HardwareAccessBlocked):
        subprocess.run([sys.executable, "-c", "print(1)"], env={}, capture_output=True)
    assert expect_blocked_access


def test_spawn_python_with_inherited_guard_env_is_allowed():
    proc = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == "1"


def test_grandchild_spawn_is_blocked_too(expect_blocked_access):
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.run(['/bin/echo', 'x'])\n"
        "    print('SPAWNED')\n"
        "except RuntimeError as e:\n"
        "    print('blocked' if 'AP-00' in str(e) else 'other')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "blocked", proc.stdout + proc.stderr
    assert _hardware_guard.drain_child_attempts()


def test_ctypes_loading_gpio_library_is_blocked(expect_blocked_access):
    import ctypes

    with pytest.raises(HardwareAccessBlocked):
        ctypes.CDLL("liblgpio.so.1")
    assert expect_blocked_access


def test_spawn_readonly_linker_query_is_allowed():
    proc = subprocess.run(["/sbin/ldconfig", "-p"], capture_output=True, timeout=30)
    assert proc.returncode == 0
    import ctypes.util

    ctypes.util.find_library("c")  # legitimer Pfad wie beim Import von ifaddr


def test_spawn_ldconfig_variants_are_blocked(expect_blocked_access):
    variants = [
        dict(args=["/sbin/ldconfig"]),                                  # kein -p
        dict(args=["/sbin/ldconfig", "-p"], executable="/bin/echo"),    # executable-Override
        dict(args=["/sbin/ldconfig", "-p"], executable="/usr/sbin/ldconfig"),  # abweichender Pfad
        dict(args=["ldconfig", "-p"]),                                  # PATH-Aufloesung
        dict(args=["/sbin/ldconfig", "-p"], env={"LD_PRELOAD": "/nonexistent.so", "PATH": "/tmp"}),
        dict(args=["/sbin/ldconfig", "-p", "-X"]),
    ]
    for kwargs in variants:
        with pytest.raises(HardwareAccessBlocked):
            subprocess.run(capture_output=True, **kwargs)
    assert len(expect_blocked_access) == len(variants)


def test_spawn_path_manipulation_does_not_resolve_ldconfig(expect_blocked_access, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(HardwareAccessBlocked):
        subprocess.run(["ldconfig", "-p"], capture_output=True)
    assert expect_blocked_access


# --- Kindprozess-Meldung an pytest -------------------------------------------

_SWALLOWING_CHILD = (
    "from hardware.digital_output import DigitalOutput\n"
    "try:\n"
    "    DigitalOutput('p', 17, True)\n"
    "except Exception:\n"
    "    pass\n"
)


def test_child_swallowed_block_is_reported_to_parent(expect_blocked_access):
    proc = subprocess.run([sys.executable, "-c", _SWALLOWING_CHILD], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    reported = _hardware_guard.drain_child_attempts()
    assert reported and "[pid " in reported[0] and "AP-00" in reported[0]


def test_child_report_file_is_not_in_production_dirs():
    log = Path(os.environ[_hardware_guard.ENV_CHILD_LOG]).resolve()
    assert not any(d in log.parents for d in _hardware_guard.PROTECTED_DIRS)
    assert log.name != ".hardware.lock"


def _run_inner_pytest(tmp_path, test_source):
    """Fuehrt einen verschachtelten pytest-Lauf mit demselben conftest aus
    (eigener Guard-Zustand), um die Fixture-Semantik zu pruefen."""
    inner = tmp_path / "test_inner.py"
    inner.write_text(test_source)
    env = {k: v for k, v in os.environ.items() if k not in (
        _hardware_guard.ENV_CHILD_LOG, _hardware_guard.ENV_PARENT_PID, _hardware_guard.ENV_LOCK_PATH)}
    tests_dir = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = os.pathsep.join([tests_dir, env["PYTHONPATH"]])
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider", "-q",
         "--rootdir", str(tmp_path), str(inner)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


_INNER_HEADER = (
    "import subprocess, sys\n"
    "CHILD = " + repr(_SWALLOWING_CHILD) + "\n"
    "def run_child():\n"
    "    p = subprocess.run([sys.executable, '-c', CHILD], capture_output=True, timeout=60)\n"
    "    assert p.returncode == 0\n"
)


def test_unexpected_child_block_fails_the_test(tmp_path):
    proc = _run_inner_pytest(tmp_path, _INNER_HEADER + "def test_x():\n    run_child()\n")
    assert proc.returncode != 0, proc.stdout
    assert "Unerwarteter Hardwarezugriff" in proc.stdout


def test_expected_child_block_can_be_acknowledged(tmp_path):
    proc = _run_inner_pytest(
        tmp_path,
        _INNER_HEADER + "def test_x(expect_blocked_access):\n    run_child()\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- Symlinks ----------------------------------------------------------------

def test_symlink_to_device_is_blocked(tmp_path, expect_blocked_access):
    link = tmp_path / "innocent"
    link.symlink_to("/dev/ttyUSB0")  # Ziel muss nicht existieren
    with pytest.raises(HardwareAccessBlocked):
        open(link, "rb")
    with pytest.raises(HardwareAccessBlocked):
        os.open(link, os.O_RDONLY)
    assert len(expect_blocked_access) == 2


def test_symlink_into_protected_dir_is_blocked(tmp_path, expect_blocked_access):
    link = tmp_path / "logs_alias"
    link.symlink_to(REPO_ROOT / "logs")
    target = link / "ap00_symlink_probe.txt"
    with pytest.raises(HardwareAccessBlocked):
        open(target, "w")
    assert not (REPO_ROOT / "logs" / "ap00_symlink_probe.txt").exists()
    assert expect_blocked_access


# --- Blocker 1: Interpreter-Optionen vollstaendig auswerten -------------------

@pytest.mark.parametrize("args", [
    ["-X", "dev", "-I", "-c", "x"],              # Review-Beispiel
    ["-W", "ignore", "-S", "-c", "x"],           # Review-Beispiel
    ["-Xdev", "-E", "-c", "x"],                  # Wert angehaengt
    ["-Wignore", "-I", "-c", "x"],
    ["-bX", "dev", "-I", "-c", "x"],             # Cluster, Wert als Folgeargument
    ["-X", "dev", "-W", "ignore", "-uE", "-c", "x"],
    ["-X", "dev", "-Sc", "x"],                   # unsicheres Flag im -c-Cluster
    ["--check-hash-based-pycs", "always", "-S", "-c", "x"],
    ["-q", "-I"],
    ["-X"],                                      # Wert fehlt
    ["--check-hash-based-pycs"],                 # Wert fehlt
    ["--unknown-option", "-c", "x"],             # unbekannt -> fail-closed
    ["-Z", "-c", "x"],
    ["-bZ", "-c", "x"],
])
def test_unsafe_flags_detects_flags_after_value_options(args):
    assert _hardware_guard._unsafe_flags(["python", *args]) is not None


@pytest.mark.parametrize("args", [
    ["-c", "x"],
    ["-cimport os"],
    ["-X", "dev", "-c", "x"],
    ["-W", "ignore", "-X", "importtime", "-c", "x"],
    ["-Xdev", "-Wignore", "-uB", "-c", "x"],
    ["-OO", "-m", "json.tool"],
    ["-m", "pytest", "-I"],                      # -I gehoert zum Modul, nicht zum Interpreter
    ["-c", "x", "-S"],                           # Programmargument
    ["script.py", "-E"],
    ["-", "-I"],
    ["--", "-I"],
    ["-X", "-I", "-c", "x"],                     # "-I" ist hier der Wert von -X (CPython getopt)
    ["--check-hash-based-pycs", "never", "-c", "x"],
    [],
])
def test_unsafe_flags_allows_legitimate_invocations(args):
    assert _hardware_guard._unsafe_flags(["python", *args]) is None


@pytest.mark.parametrize("prefix", [["-X", "dev", "-I"], ["-W", "ignore", "-S"], ["-Xdev", "-E"], ["-Wignore", "-IS"]])
def test_spawn_python_bypass_flag_after_value_option_is_blocked(prefix, expect_blocked_access):
    with pytest.raises(HardwareAccessBlocked):
        subprocess.run([sys.executable, *prefix, "-c", "print(1)"], capture_output=True)
    assert expect_blocked_access


def test_spawn_python_with_value_options_still_runs_guarded():
    code = "import os; print(os.environ.get('CDS_TEST_HARDWARE_GUARD'), 'gpiozero' not in __import__('sys').modules)"
    proc = subprocess.run([sys.executable, "-X", "dev", "-W", "ignore", "-c", code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["1", "True"]


# --- Blocker 2: fehlgeschlagene Kindprozess-Meldung ist fail-closed ------------

class _Exited(BaseException):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@pytest.mark.parametrize("failure", ["write_error", "missing_path"])
def test_report_failure_terminates_child_instead_of_ignoring(failure, tmp_path, monkeypatch, capfd,
                                                             expect_blocked_access):
    monkeypatch.setenv(_hardware_guard.ENV_PARENT_PID, "0")  # dieser Prozess gilt als Kind
    monkeypatch.setenv(_hardware_guard.ENV_CHILD_LOG, str(tmp_path / "child.log"))
    if failure == "write_error":
        def failing_open(*_a, **_k):
            raise OSError(28, "No space left on device (simuliert)")
        monkeypatch.setattr(_hardware_guard, "_REAL_OPEN", failing_open)
    else:
        monkeypatch.delenv(_hardware_guard.ENV_CHILD_LOG)

    def fake_exit(code):
        raise _Exited(code)
    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(_Exited) as exc_info:
        _hardware_guard._block("simulierter GPIO-Zugriff")
    assert exc_info.value.code == _hardware_guard.REPORT_FAILURE_EXIT_CODE
    assert "Meldung an pytest fehlgeschlagen" in capfd.readouterr().err


_SWALLOWING_CHILD_CONTINUES = _SWALLOWING_CHILD + "print('CONTINUED')\n"


def test_child_swallowing_block_with_failed_report_is_detected(tmp_path, expect_blocked_access):
    env = dict(os.environ)
    env[_hardware_guard.ENV_CHILD_LOG] = str(tmp_path / "missing_dir" / "child.log")  # nicht beschreibbar
    proc = subprocess.run([sys.executable, "-c", _SWALLOWING_CHILD_CONTINUES], cwd=str(REPO_ROOT), env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == _hardware_guard.REPORT_FAILURE_EXIT_CODE, proc.stdout + proc.stderr
    assert "CONTINUED" not in proc.stdout  # Kind laeuft nach der verschluckten Blockade nicht weiter
    assert "Meldung an pytest fehlgeschlagen" in proc.stderr
    assert any("konnte eine Blockade nicht melden" in a for a in expect_blocked_access)
    assert not (tmp_path / "missing_dir").exists()


def test_failed_child_report_fails_test_even_if_returncode_is_ignored(tmp_path):
    source = (
        "import os, subprocess, sys\n"
        "CHILD = " + repr(_SWALLOWING_CHILD) + "\n"
        "def test_x(tmp_path):\n"
        "    env = dict(os.environ)\n"
        "    env['" + _hardware_guard.ENV_CHILD_LOG + "'] = str(tmp_path / 'missing' / 'c.log')\n"
        "    subprocess.run([sys.executable, '-c', CHILD], env=env, capture_output=True, timeout=60)\n"
    )
    proc = _run_inner_pytest(tmp_path, source)
    assert proc.returncode != 0, proc.stdout
    assert "konnte eine Blockade nicht melden" in proc.stdout


# --- os.open mit dir_fd --------------------------------------------------------

@pytest.mark.parametrize("directory,name", [("/dev", "ttyUSB0"), ("/dev", "ttyACM0"), ("/dev", "gpiochip0")])
def test_os_open_device_via_dir_fd_is_blocked(directory, name, expect_blocked_access):
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(HardwareAccessBlocked):
            os.open(name, os.O_RDWR, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    assert expect_blocked_access


def test_os_open_with_unresolvable_dir_fd_is_blocked(expect_blocked_access):
    with pytest.raises(HardwareAccessBlocked):
        os.open("ttyUSB0", os.O_RDONLY, dir_fd=987654)


def test_os_open_with_dir_fd_in_tmp_still_works(tmp_path):
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("probe.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=dir_fd)
        os.close(fd)
    finally:
        os.close(dir_fd)
    assert (tmp_path / "probe.txt").exists()
