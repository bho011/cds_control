"""
Wird in Test-Subprozessen ueber PYTHONPATH geladen (siehe tests/conftest.py)
und installiert dieselbe Hardware-Isolation wie im Hauptprozess. Danach wird
die System-sitecustomize (apport) weiter ausgefuehrt.
"""
import os
import sys

if os.environ.get("CDS_TEST_HARDWARE_GUARD") == "1":
    _tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _inserted = _tests_dir not in sys.path
    if _inserted:
        sys.path.insert(0, _tests_dir)
    import _hardware_guard

    _hardware_guard.install()
    if _inserted:
        sys.path.remove(_tests_dir)

_here = os.path.dirname(os.path.abspath(__file__))
for _p in sys.path:
    _cand = os.path.join(_p or ".", "sitecustomize.py")
    if os.path.abspath(os.path.dirname(_cand)) != _here and os.path.isfile(_cand):
        with open(_cand) as _f:
            exec(compile(_f.read(), _cand, "exec"))
        break
