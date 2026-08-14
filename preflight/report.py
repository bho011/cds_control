"""Sammel-Ergebnistyp für den Preflight-Check: OK/WARN/FAIL je Prüfung."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


class PreflightReport:
    def __init__(self):
        self.results: list[CheckResult] = []

    def ok(self, name: str, detail: str = ""):
        self.results.append(CheckResult(name, "OK", detail))

    def warn(self, name: str, detail: str = ""):
        self.results.append(CheckResult(name, "WARN", detail))

    def fail(self, name: str, detail: str = ""):
        self.results.append(CheckResult(name, "FAIL", detail))

    @property
    def has_failures(self) -> bool:
        return any(result.status == "FAIL" for result in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(result.status == "WARN" for result in self.results)

    def print_report(self):
        print()
        print("CDS Preflight Check")
        print("===================")

        for result in self.results:
            print(f"[{result.status:<4}] {result.name}")
            if result.detail:
                print(f"       {result.detail}")

        print()
        if self.has_failures:
            print("RESULT: FAIL - Nicht starten, erst Fehler beheben.")
        elif self.has_warnings:
            print("RESULT: WARN - Grundsätzlich lauffähig, Warnungen prüfen.")
        else:
            print("RESULT: OK - Preflight erfolgreich.")

    def format_report_lines(self) -> list[str]:
        """Same content as print_report(), returned as lines instead of printed - used by the dashboard's Preflight button to feed the Process Log."""
        lines = ["CDS Preflight Check", "==================="]

        for result in self.results:
            lines.append(f"[{result.status:<4}] {result.name}")
            if result.detail:
                lines.append(f"       {result.detail}")

        lines.append("")
        if self.has_failures:
            lines.append("RESULT: FAIL - Nicht starten, erst Fehler beheben.")
        elif self.has_warnings:
            lines.append("RESULT: WARN - Grundsätzlich lauffähig, Warnungen prüfen.")
        else:
            lines.append("RESULT: OK - Preflight erfolgreich.")

        return lines
