"""Phasen-Konstanten für Tank Cleaning."""

from __future__ import annotations


class TankCleaningPhase:
    """Human-readable phase names, also used as the published process_state."""

    IDLE = "IDLE"
    FILLING = "FILLING"
    HOLDING = "HOLDING"
    DRAINING = "DRAINING"
    FINISHED = "FINISHED"
    ERROR = "ERROR"
