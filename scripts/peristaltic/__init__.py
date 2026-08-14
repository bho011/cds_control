"""CLI subcommand implementations for scripts/peristaltic_calibration_cli.py.

Nur der 'discover'-Unterbefehl (discover.py) ist hier ausgelagert, weil er
komplett eigenständig ist (keine geteilten Pfade, keine Mapping-/Firmware-
Abhängigkeit). Alle anderen Unterbefehle bleiben bewusst im Wrapper-Skript
selbst - siehe dessen Modul-Docstring für den genauen Grund
(Test-Monkeypatching von Modul-Attributen wie MAPPING_PATH).
"""
