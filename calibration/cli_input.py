"""Interaktive Konsoleneingabe-Hilfsfunktionen für die Kalibrier-Session."""

from __future__ import annotations

from typing import Optional


def parse_float_input(text: str) -> float:
    normalized = text.strip().replace(",", ".")
    return float(normalized)


def ask_float(prompt: str, default: Optional[float] = None) -> float:
    while True:
        default_text = f" [{default}]" if default is not None else ""
        user_input = input(f"{prompt}{default_text}: ").strip()

        if user_input == "" and default is not None:
            return default

        try:
            return parse_float_input(user_input)
        except ValueError:
            print("Ungültige Eingabe. Bitte Zahl eingeben, z. B. 5 oder 5,0.")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    default_text = "J/n" if default else "j/N"

    while True:
        user_input = input(f"{prompt} [{default_text}]: ").strip().lower()

        if user_input == "":
            return default

        if user_input in ("j", "ja", "y", "yes"):
            return True

        if user_input in ("n", "nein", "no"):
            return False

        print("Bitte j oder n eingeben.")
