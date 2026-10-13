"""
Zentrale, fail-closed Hardware-Isolation fuer die Testsuite (AP-00).

Wird ausschliesslich ueber tests/conftest.py (Modulebene, also VOR der
Collection) und - fuer Subprozesse - ueber tests/_guard_site/sitecustomize.py
installiert. Aenderungen an Produktivcode sind nicht noetig.

Geschuetzte Zugriffspfade:
  * GPIO:    gpiozero.Device.pin_factory (Blocker), lgpio.gpiochip_open & Co.
  * Serial:  serial.Serial.open (pyserial, posix) sowie os.open auf Geraetepfade
  * Lock:    hardware.actuator_manager.LOCK_PATH -> temporaere Test-Sperrdatei
  * Daten:   Schreibzugriffe auf logs/, config/, recipes/, calibration_data/
  * Netz:    socket.connect (MQTT/OPC-UA, Node-RED reagiert auf MQTT)

Jeder blockierte Versuch wird in BLOCKED_ATTEMPTS protokolliert, auch wenn
der Aufrufer die Exception verschluckt; die autouse-Fixture in conftest.py
laesst den Test dann scheitern. Kindprozesse melden ueber eine temporaere
Datei; scheitert die Meldung, beendet sich das Kind mit
REPORT_FAILURE_EXIT_CODE, den der Elternprozess als Blockade zaehlt.

Bekannte technische Grenzen (keine Hardwarefreigabe, nur Meldelucken):
  * Den Exit-Code eines Kindes sieht der Elternprozess nur, wenn er dessen
    Status abfragt (run/wait/poll/communicate). Ein nie abgewartetes Kind
    bleibt bei fehlgeschlagener Meldung unbemerkt - der Hardwarezugriff
    selbst ist trotzdem blockiert.
  * Ein Kind, dem der Test ENV_CHILD_LOG/ENV_PARENT_PID ausdruecklich
    entzieht, wird eigene Wurzel (so beabsichtigt fuer verschachtelte
    pytest-Laeufe); dessen Ergebnis zaehlt nur ueber den Exit-Code.
  * Schreibschutz der Produktivverzeichnisse gilt nicht fuer Aufrufe mit
    dir_fd/src_dir_fd/dst_dir_fd; Geraetepfade sind bei os.open mit dir_fd
    dagegen abgedeckt.
"""

from __future__ import annotations

import builtins
import importlib.abc
import importlib.machinery
import inspect
import io
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTECTED_DIRS = tuple(REPO_ROOT / name for name in ("logs", "config", "recipes", "calibration_data"))
_REAL_PROTECTED = tuple(Path(os.path.realpath(d)) for d in PROTECTED_DIRS)
DEVICE_PREFIXES = (
    "/dev/ttyUSB", "/dev/ttyACM", "/dev/ttyAMA", "/dev/ttyS", "/dev/serial/",
    "/dev/gpiochip", "/dev/gpiomem", "/dev/mem", "/dev/spidev", "/dev/i2c",
)
ENV_LOCK_PATH = "CDS_TEST_HARDWARE_LOCK_PATH"
ENV_ACTIVE = "CDS_TEST_HARDWARE_GUARD"
ENV_CHILD_LOG = "CDS_TEST_BLOCKED_LOG"   # temporaere Meldedatei Kind -> pytest-Hauptprozess
ENV_PARENT_PID = "CDS_TEST_GUARD_ROOT_PID"
_REAL_OPEN = builtins.open  # vor der Installation der Guards gebunden
# Exit-Code eines Kindprozesses, dessen Blockade-Meldung nicht geschrieben
# werden konnte - der Elternprozess wertet ihn als blockierten Versuch.
REPORT_FAILURE_EXIT_CODE = 86

BLOCKED_ATTEMPTS: list[str] = []
_state_lock = threading.Lock()
_installed = False
_session_tmpdir: tempfile.TemporaryDirectory | None = None


class HardwareAccessBlocked(RuntimeError):
    """Ein Test hat versucht, echte Hardware / Produktivdaten zu erreichen."""


def _report_to_root(message: str) -> None:
    """Kindprozesse melden Blockaden ueber eine temporaere Datei (nie in
    Produktivverzeichnissen) an den pytest-Hauptprozess, auch wenn sie die
    Exception verschlucken.

    Fail-closed: Schlaegt die Meldung fehl (fehlender Pfad, Schreibfehler),
    beendet sich das Kind sofort mit REPORT_FAILURE_EXIT_CODE - es kann die
    Blockade dann nicht mehr verschlucken und weiterlaufen. Der Elternprozess
    erkennt diesen Exit-Code (siehe _install_exit_status_guard)."""
    if os.environ.get(ENV_PARENT_PID) == str(os.getpid()):
        return  # pytest-Hauptprozess: BLOCKED_ATTEMPTS genuegt
    path = os.environ.get(ENV_CHILD_LOG)
    try:
        if not path:
            raise OSError(f"{ENV_CHILD_LOG} nicht gesetzt")
        with _REAL_OPEN(path, "a", encoding="utf-8") as fh:
            fh.write(f"[pid {os.getpid()}] {message}".replace("\n", " ") + "\n")
    except Exception as exc:
        _abort_child_after_report_failure(message, exc)


def _abort_child_after_report_failure(message: str, exc: Exception) -> None:
    try:
        os.write(2, (f"[AP-00 Hardware-Guard] Meldung an pytest fehlgeschlagen ({exc!r}); "
                     f"Kindprozess wird beendet. Blockade: {message}\n").encode("utf-8", "replace"))
    finally:
        os._exit(REPORT_FAILURE_EXIT_CODE)


def drain_child_attempts() -> list[str]:
    """Uebernimmt gemeldete Kindprozess-Blockaden in BLOCKED_ATTEMPTS."""
    path = os.environ.get(ENV_CHILD_LOG)
    if not path or not os.path.exists(path):
        return []
    with _REAL_OPEN(path, "r+", encoding="utf-8") as fh:
        lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
        fh.seek(0)
        fh.truncate()
    with _state_lock:
        BLOCKED_ATTEMPTS.extend(lines)
    return lines


def _record(message: str) -> None:
    with _state_lock:
        BLOCKED_ATTEMPTS.append(message)
    _report_to_root(message)


def _block(what: str):
    message = f"[AP-00 Hardware-Guard] blockiert: {what}"
    _record(message)
    raise HardwareAccessBlocked(message)


def _is_device_path(path) -> bool:
    try:
        text = os.fspath(path)
    except TypeError:
        return False
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if text.startswith(DEVICE_PREFIXES):
        return True
    try:  # Symlinks auf Geraete (realpath ist rein lesend)
        return os.path.realpath(text).startswith(DEVICE_PREFIXES)
    except (OSError, ValueError):
        return False


def _is_protected(path) -> bool:
    try:
        resolved = Path(os.path.realpath(os.fspath(path)))
    except (TypeError, ValueError, OSError):
        return False
    return any(resolved == d or d in resolved.parents for d in _REAL_PROTECTED)


_WRITE_MODE_CHARS = set("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


# --- GPIO --------------------------------------------------------------------

class _BlockingPinFactory:
    """Pin-Factory, die jeden Zugriff verweigert (statt zu simulieren)."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        _block(f"gpiozero pin factory: {name}")

    # Von gpiozero beim Interpreter-Ende (atexit) benoetigt - kein Hardwarezugriff.
    _res_lock = threading.RLock()
    _reservations: dict = {}

    def close(self):
        return None


def _install_gpiozero(module) -> None:
    device = module.Device
    device.pin_factory = _BlockingPinFactory()
    device._default_pin_factory = staticmethod(lambda: _block("gpiozero default pin factory"))
    device.ensure_pin_factory = staticmethod(lambda: _block("gpiozero ensure_pin_factory"))


def _install_lgpio(module) -> None:
    for name in dir(module):
        if name.startswith(("gpiochip_", "gpio_claim", "gpio_write", "gpio_read", "group_claim", "tx_")):
            def blocked(*_a, _n=name, **_k):
                _block(f"lgpio.{_n}")
            setattr(module, name, blocked)


# --- Serial ------------------------------------------------------------------

def _install_serial(module) -> None:
    import serial.serialposix as serialposix

    def blocked_open(self, *_a, **_k):
        _block(f"serial.Serial.open(port={getattr(self, 'port', None)!r})")

    serialposix.Serial.open = blocked_open
    module.Serial.open = blocked_open


# --- Lock --------------------------------------------------------------------

def session_lock_path() -> Path:
    env = os.environ.get(ENV_LOCK_PATH)
    if env:
        return Path(env)
    global _session_tmpdir
    if _session_tmpdir is None:
        _session_tmpdir = tempfile.TemporaryDirectory(prefix="cds-test-lock-")
    path = Path(_session_tmpdir.name) / "hardware.lock"
    os.environ[ENV_LOCK_PATH] = str(path)
    return path


def _install_actuator_manager(module) -> None:
    module.LOCK_PATH = session_lock_path()


# --- Dateisystem / Netz ------------------------------------------------------

def _install_fs_guards() -> None:
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)):
            if _is_device_path(file):
                _block(f"open({os.fspath(file)!r})")
            if _WRITE_MODE_CHARS & set(mode) and _is_protected(file):
                _block(f"write open({os.fspath(file)!r}, {mode!r}) auf Produktivdaten")
        return real_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_open

    real_os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if _is_device_path(path):
            _block(f"os.open({os.fspath(path)!r})")
        dir_fd = kwargs.get("dir_fd")
        if dir_fd is not None and not os.path.isabs(os.fsdecode(path)):
            # Relativ zu einem Verzeichnis-FD (z.B. von /dev): Verzeichnis ueber
            # /proc aufloesen; nicht aufloesbar -> fail-closed.
            try:
                base = os.readlink(f"/proc/self/fd/{int(dir_fd)}")
            except (OSError, TypeError, ValueError):
                _block(f"os.open({os.fspath(path)!r}, dir_fd={dir_fd!r}): Verzeichnis nicht aufloesbar")
            if _is_device_path(os.path.join(base, os.fsdecode(path))):
                _block(f"os.open({os.fspath(path)!r}, dir_fd -> {base!r})")
        # Mit dir_fd ist der Pfad relativ zu einem Verzeichnis-FD, nicht zum cwd.
        if flags & _WRITE_FLAGS and kwargs.get("dir_fd") is None and _is_protected(path):
            _block(f"os.open({os.fspath(path)!r}) schreibend auf Produktivdaten")
        return real_os_open(path, flags, *args, **kwargs)

    os.open = guarded_os_open

    for fn_name in ("replace", "rename", "remove", "unlink", "rmdir", "mkdir", "makedirs"):
        real = getattr(os, fn_name)

        def make(real=real, fn_name=fn_name):
            def guarded(*paths, **kwargs):
                if any(kwargs.get(k) is not None for k in ("dir_fd", "src_dir_fd", "dst_dir_fd")):
                    return real(*paths, **kwargs)
                n = 2 if fn_name in ("replace", "rename") else 1
                for p in paths[:n]:
                    if not (isinstance(p, (str, bytes, os.PathLike)) and _is_protected(p)):
                        continue
                    # Anlegen eines bereits vorhandenen Verzeichnisses aendert nichts.
                    if fn_name in ("mkdir", "makedirs") and os.path.exists(p):
                        continue
                    _block(f"os.{fn_name}({os.fspath(p)!r}) auf Produktivdaten")
                return real(*paths, **kwargs)
            return guarded

        setattr(os, fn_name, make())


def _install_network_guard() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        _block(f"socket.connect({address!r})")

    def connect_ex(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect_ex(self, address)
        _block(f"socket.connect_ex({address!r})")

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


# --- Kindprozesse ------------------------------------------------------------

_GUARD_SITE = str(Path(__file__).resolve().parent / "_guard_site")
# Interpreter-Optionen nach CPython 3.11 (Python/getopt.c, `python -h`).
# -I/-E ignorieren PYTHONPATH/PYTHON*-Variablen, -S ueberspringt sitecustomize.
_UNSAFE_PY_FLAGS = set("ISE")
_PROGRAM_FLAGS = set("cm")          # Rest des Tokens bzw. Folgeargument ist Programm -> Optionsende
_VALUE_FLAGS = set("WX")            # Wert direkt angehaengt (-Xdev) oder als Folgeargument (-X dev)
_PLAIN_FLAGS = set("bBdhiOPqRsuvVx?")
_LONG_VALUE_OPTIONS = {"--check-hash-based-pycs"}
_LONG_PLAIN_OPTIONS = {"--help", "--version", "--help-env", "--help-xoptions", "--help-all"}


# Rein lesende Linker-Abfrage (ctypes.util.find_library, beim Import von
# ifaddr/nicegui): nur dieses kanonische Binary, absolut, ohne PATH-Aufloesung.
_LDCONFIG_CANONICAL = os.path.realpath("/sbin/ldconfig")
_LDCONFIG_ARGV = ("/sbin/ldconfig", "-p")
_DANGEROUS_ENV_PREFIXES = ("LD_",)


def _is_allowed_linker_query(program, argv, env) -> bool:
    if tuple(os.fsdecode(a) for a in argv) != _LDCONFIG_ARGV:
        return False
    program = os.fsdecode(program)
    if program != _LDCONFIG_ARGV[0]:  # kein executable-Override, kein PATH-Lookup
        return False
    if not os.path.exists(_LDCONFIG_CANONICAL) or os.path.realpath(program) != _LDCONFIG_CANONICAL:
        return False
    effective = os.environ if env is None else env
    return not any(str(k).startswith(_DANGEROUS_ENV_PREFIXES) for k in effective)


def _same_python(program) -> bool:
    try:
        resolved = os.path.realpath(os.fsdecode(program))
        own = os.path.realpath(sys.executable)
        if resolved == own:
            return True
        # Venv-Wrapper (python -> python3 -> system binary) gleichsetzen.
        return os.path.samefile(resolved, own)
    except (OSError, TypeError, ValueError):
        return False


def _unsafe_flags(argv) -> str | None:
    """Prueft ALLE Interpreter-Optionen vor -c/-m/Skript/'-'/'--'. Liefert die
    beanstandete Option oder None. Unbekannte Optionen und fehlende Werte
    gelten fail-closed als unsicher."""
    args = [os.fsdecode(a) for a in list(argv)[1:]]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--" or arg == "-" or not arg.startswith("-"):
            return None  # Optionsende: Skript bzw. stdin folgt
        if arg.startswith("--"):
            if arg in _LONG_VALUE_OPTIONS:
                if i + 1 >= len(args):
                    return f"{arg} (Wert fehlt)"
                i += 2
                continue
            if arg in _LONG_PLAIN_OPTIONS:
                i += 1
                continue
            return f"{arg} (unbekannte Option)"
        consumed_next = False
        for pos in range(1, len(arg)):
            ch = arg[pos]
            if ch in _UNSAFE_PY_FLAGS:
                return f"-{ch}"
            if ch in _PROGRAM_FLAGS:
                return None
            if ch in _VALUE_FLAGS:
                if pos + 1 == len(arg):  # Wert ist das Folgeargument
                    if i + 1 >= len(args):
                        return f"-{ch} (Wert fehlt)"
                    consumed_next = True
                break  # Rest des Tokens ist der Wert, keine Optionen mehr
            if ch not in _PLAIN_FLAGS:
                return f"-{ch} (unbekannte Option)"
        i += 2 if consumed_next else 1
    return None


def _check_spawn(what: str, program, argv, env) -> None:
    """Erlaubt nur den eigenen Python-Interpreter mit geerbtem Guard."""
    shown = list(argv) if argv is not None else [program]
    if _is_allowed_linker_query(program, shown, env):
        return
    if not _same_python(program):
        _block(f"{what}: externer Prozess {shown!r} (nur der Test-Interpreter ist erlaubt)")
    flag = _unsafe_flags(shown)
    if flag:
        _block(f"{what}: Python-Flag {flag} umgeht den Guard ({shown!r})")
    effective = os.environ if env is None else env
    if effective.get(ENV_ACTIVE) != "1" or _GUARD_SITE not in str(effective.get("PYTHONPATH", "")).split(os.pathsep):
        _block(f"{what}: Umgebung ohne Guard (CDS_TEST_HARDWARE_GUARD/PYTHONPATH) ({shown!r})")


def _install_spawn_guards() -> None:
    import subprocess

    real_execute = subprocess.Popen._execute_child
    # Versionsabhaengig (CPython-intern): Signatur pruefen, sonst fail-closed.
    sig = inspect.signature(real_execute)
    missing = {"args", "executable", "env", "shell"} - set(sig.parameters)
    if missing:
        raise RuntimeError(f"[AP-00 Hardware-Guard] Popen._execute_child unbekannt, fehlt: {sorted(missing)}")

    def execute_child(self, *call_args, **call_kwargs):
        bound = sig.bind(self, *call_args, **call_kwargs).arguments
        args, executable = bound["args"], bound.get("executable")
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        if bound.get("shell"):
            _block(f"subprocess shell=True: {args!r}")
        _check_spawn("subprocess", executable or argv[0], argv, bound.get("env"))
        return real_execute(self, *call_args, **call_kwargs)

    subprocess.Popen._execute_child = execute_child
    _install_exit_status_guard(subprocess)

    def blocked_system(command):
        _block(f"os.system({command!r})")

    os.system = blocked_system

    for name in ("execv", "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            def make(name=name):
                def blocked(*a, **k):
                    _block(f"os.{name}{a[:2]!r}")
                return blocked
            setattr(os, name, make())
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                 "execl", "execle", "execlp", "execlpe"):
        if hasattr(os, name):
            def make(name=name):
                def blocked(*a, **k):
                    _block(f"os.{name}{a[:2]!r}")
                return blocked
            setattr(os, name, make())


def _install_exit_status_guard(subprocess) -> None:
    """Wertet REPORT_FAILURE_EXIT_CODE eines Kindes als blockierten Versuch
    (Kind konnte seine Blockade nicht melden). Greift, sobald der Exit-Status
    beobachtet wird (wait/poll/communicate/run). CPython-intern, daher
    Signaturpruefung und sonst fail-closed."""
    real_handle = getattr(subprocess.Popen, "_handle_exitstatus", None)
    if real_handle is None or "sts" not in inspect.signature(real_handle).parameters:
        raise RuntimeError("[AP-00 Hardware-Guard] Popen._handle_exitstatus unbekannt")

    def handle_exitstatus(self, *args, **kwargs):
        result = real_handle(self, *args, **kwargs)
        if self.returncode == REPORT_FAILURE_EXIT_CODE:
            _record(
                f"[AP-00 Hardware-Guard] Kindprozess pid {self.pid} konnte eine Blockade nicht melden "
                f"(Exit-Code {REPORT_FAILURE_EXIT_CODE}, args={self.args!r})"
            )
        return result

    subprocess.Popen._handle_exitstatus = handle_exitstatus


def _install_native_loader_guard() -> None:
    """Blockiert ctypes-Laden GPIO-naher Bibliotheken (kein Schutz gegen Pfad-Umgehung)."""
    import ctypes

    real_init = ctypes.CDLL.__init__

    def init(self, name, *a, **k):
        if name and any(t in os.fsdecode(name).lower() for t in ("lgpio", "pigpio", "gpiod", "wiringpi", "rpi")):
            _block(f"ctypes.CDLL({name!r})")
        return real_init(self, name, *a, **k)

    ctypes.CDLL.__init__ = init


# --- Post-Import-Hook (greift auch fuer spaeter importierte Module) ----------

_POST_IMPORT = {
    "gpiozero": _install_gpiozero,
    "lgpio": _install_lgpio,
    "serial": _install_serial,
    "hardware.actuator_manager": _install_actuator_manager,
}


class _PostImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in _POST_IMPORT:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            spec = find(fullname, path, target) if find else None
            if spec is not None and spec.loader is not None:
                spec.loader = _PatchingLoader(spec.loader, _POST_IMPORT[fullname])
                return spec
        return None


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, inner, patch):
        self._inner = inner
        self._patch = patch

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        self._patch(module)


def install() -> None:
    """Idempotent. Muss vor dem Import hardwarenaher Module laufen."""
    global _installed
    if _installed:
        return
    _installed = True

    os.environ[ENV_ACTIVE] = "1"
    os.environ["GPIOZERO_PIN_FACTORY"] = "mock"  # Rueckfallebene fuer Subprozesse
    session_lock_path()
    os.environ.setdefault(ENV_PARENT_PID, str(os.getpid()))
    if not os.environ.get(ENV_CHILD_LOG):
        os.environ[ENV_CHILD_LOG] = str(session_lock_path().parent / "child-blocked.log")

    _install_fs_guards()
    _install_network_guard()
    _install_spawn_guards()
    _install_native_loader_guard()

    # Bereits importierte Module sofort patchen, den Rest per Import-Hook.
    for name, patch in _POST_IMPORT.items():
        if name in sys.modules:
            patch(sys.modules[name])
    sys.meta_path.insert(0, _PostImportFinder())
