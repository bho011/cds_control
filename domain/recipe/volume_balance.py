"""Reines Volumenmodell fuer die rechnerische Bewertung eines Mischlaufs mit
Naehrstoffdosierung (AP-05 Stufe 1).

Keine Hardware, kein I/O, keine Sensorlesung, keine Zeit, keine Aktorik: das
Modul bewertet ausschliesslich uebergebene, unveraenderliche Werte und liefert
typisierte Ergebnisobjekte mit maschinenlesbarem Fehlergrund. Es schaltet nie
eine Pumpe, korrigiert nie einen Wert und fuehrt keine stillen Toleranzen ein.

Vier Evidenzklassen, die nie vermischt werden (siehe ``VOLUME_FIELD_EVIDENCE``):

1. PLANNED          - rechnerisch geplant (Rezept/RunConfigSnapshot)
2. PROTOCOL_CONFIRMED - per MCU-Protokoll (DONE) bestaetigt. Ein DONE ist ein
                       Protokollereignis (Schrittzaehler erreicht), KEIN
                       physischer Foerdermengennachweis.
3. SENSOR_MEASURED  - vom Mixer-Fuellstandssensor gemessen (Gesamtvolumen,
                       ohne Aufspaltung in RO und Naehrstoff)
4. UNCONFIRMED      - gestartet, aber nicht bestaetigt (in flight) bzw.
                       abgebrochen/unsicher

Es gibt KEINE unabhaengige RO-Durchflussmessung. Die RO-Menge ist nur indirekt
schaetzbar (``indirect_ro_estimate_l``); dieser Wert ist kein Nachweis und wird
von keiner Pruefung als Grenzwert verwendet.

Exaktheit: Naehrstoffmengen werden intern in ganzzahligen Mikrolitern (uL)
gefuehrt, Fuellstands-/Grenzvergleiche laufen exakt (``fractions.Fraction``)
ohne Gleitkommafehler. Die Kapazitaetsangabe wird zu ganzen uL abgerundet und
kann die verfuegbare Kapazitaet daher nur unter-, nie ueberschaetzen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Any

from .calculations import compute_base_volume_before_ro_correction_l, round_l
from .limits import MAX_PROCESS_VOLUME_L, MAX_RECIPE_RO_WATER_L

UL_PER_ML = 1000
UL_PER_L = 1_000_000

# Darstellbarkeit, KEINE physikalische Toleranz: ein ml-Wert gilt nur dann als
# ganzzahliges uL-Vielfaches, wenn er davon hoechstens um Gleitkomma-Rauschen
# (in uL gemessen) abweicht, z. B. 12.345000000000001 ml -> 12345 uL.
_UL_REPRESENTATION_EPS = 1e-6


class Evidence(str, Enum):
    PLANNED = "planned"
    PROTOCOL_CONFIRMED = "protocol_confirmed"
    SENSOR_MEASURED = "sensor_measured"
    UNCONFIRMED = "unconfirmed"


class VolumeReason(str, Enum):
    """Maschinenlesbare Fehlergruende (stabile Werte, nicht uebersetzt)."""

    INVALID_INPUT = "invalid_input"
    NEGATIVE_VALUE = "negative_value"
    NOT_QUANTIZABLE = "not_quantizable"
    RO_TARGET_INVALID = "ro_target_invalid"
    RO_TARGET_ABOVE_LIMIT = "ro_target_above_limit"
    MAX_MIXER_INVALID = "max_mixer_invalid"
    ESTIMATE_MISMATCH = "estimate_mismatch"
    PROCESS_VOLUME_ABOVE_LIMIT = "process_volume_above_limit"
    PREFILL_TARGET_MISSING = "prefill_target_missing"
    PREFILL_TARGET_INVALID = "prefill_target_invalid"
    CONFIRMED_EXCEEDS_PLANNED = "confirmed_exceeds_planned"
    NUTRIENT_ACCOUNTING_INCONSISTENT = "nutrient_accounting_inconsistent"
    MIXER_ABOVE_LIMIT = "mixer_above_limit"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    PHASE_TARGET_INVALID = "phase_target_invalid"
    FROZEN_TARGET_MISMATCH = "frozen_target_mismatch"
    CONFIRMED_DECREASED = "confirmed_decreased"
    UNCERTAIN_NUTRIENT_PRESENT = "uncertain_nutrient_present"
    DOSING_INCOMPLETE = "dosing_incomplete"
    FINAL_LEVEL_BELOW_TARGET = "final_level_below_target"
    NO_DOSING_PLANNED = "no_dosing_planned"
    RESUME_RO_ON_COUNT_INVALID = "resume_ro_on_count_invalid"
    RESUME_UNCERTAIN_NUTRIENT = "resume_uncertain_nutrient"
    RESUME_PROGRESS_BELOW_THRESHOLD = "resume_progress_below_threshold"
    RESUME_LEVEL_BELOW_BASELINE = "resume_level_below_baseline"


class VolumeModelError(ValueError):
    """Wird nur bei der Konstruktion eines ungueltigen ``VolumePlan`` bzw. bei
    ungueltigen Argumenten der Helfer geworfen. Die Bewertungsfunktionen
    (``evaluate_*``) werfen NICHT, sie liefern einen Fehlergrund."""

    def __init__(self, reason: VolumeReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# --- Helfer --------------------------------------------------------------------


def safe_repr(value: Any, limit: int = 80) -> str:
    """Fehlertext-tauglicher, immer terminierender Repr. Sehr grosse
    Ganzzahlen werden NICHT in Dezimalziffern umgewandelt (das wuerde bei mehr
    als sys.get_int_max_str_digits() Stellen ein ValueError ausloesen; diese
    Python-Sicherheitsgrenze bleibt unveraendert aktiv), sondern nur mit ihrer
    Bitlaenge beschrieben. Lange oder nicht darstellbare Werte werden gekuerzt."""
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            if value.bit_length() > 256:
                return f"<Ganzzahl mit {value.bit_length()} Bit>"
            return repr(value)
        text = repr(value)
    except Exception:  # noqa: BLE001 - Fehlerformatierung darf nie selbst scheitern
        return f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[:limit] + "..."


def is_finite_number(value: Any) -> bool:
    """Echte, endliche und als float darstellbare Zahl (kein bool, kein str).
    Sehr grosse Ganzzahlen (z. B. 10**400) sind nicht darstellbar und werden
    abgelehnt, ohne OverflowError nach aussen."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


_is_number = is_finite_number


def ml_to_ul(value: Any, name: str = "ml-Wert") -> int:
    """Strikte Umrechnung ml -> ganze uL. Lehnt bool, str, NaN/Inf, negative
    und nicht auf 1 uL darstellbare Werte ab (keine stille Korrektur)."""
    if not _is_number(value):
        raise VolumeModelError(VolumeReason.INVALID_INPUT, f"{name} muss eine endliche Zahl sein (war: {safe_repr(value)}).")
    if value < 0:
        raise VolumeModelError(VolumeReason.NEGATIVE_VALUE, f"{name} darf nicht negativ sein (war: {safe_repr(value)}).")
    scaled = value * UL_PER_ML
    if isinstance(scaled, float) and not math.isfinite(scaled):
        raise VolumeModelError(VolumeReason.INVALID_INPUT, f"{name} ist zu gross (war: {safe_repr(value)}).")
    ul = round(scaled)
    if abs(scaled - ul) > _UL_REPRESENTATION_EPS:
        raise VolumeModelError(
            VolumeReason.NOT_QUANTIZABLE, f"{name} ist nicht auf 1 uL darstellbar (war: {safe_repr(value)} ml)."
        )
    return int(ul)


def ul_to_ml(ul: int) -> float:
    try:
        return ul / UL_PER_ML
    except OverflowError as exc:
        raise VolumeModelError(VolumeReason.INVALID_INPUT, "uL-Wert ist als ml-Zahl nicht darstellbar.") from exc


def _frac(value: float) -> Fraction:
    return Fraction(value)  # exakt, nur fuer endliche Zahlen aufrufen


# --- Plan ------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumePlan:
    """Rechnerisch geplante Groessen eines Laufs - unveraenderlich.

    ``target_ro_water_l``           nur RO-Wasser (Rezept)
    ``estimated_process_volume_l``  Rezept-Gesamtvolumen = RO + Naehrstoffe
    ``planned_nutrient_ml``         geplante Gesamtmenge Naehrstoffloesung
    ``max_mixer_l``                 operative Mixergrenze (<= 185 l)
    ``prefill_target_l``            Vorfuellziel (Parameter, vorlaeufig); Pflicht
                                    bei geplanter Dosierung
    """

    target_ro_water_l: float
    estimated_process_volume_l: float
    planned_nutrient_ml: float
    max_mixer_l: float
    prefill_target_l: float | None = None

    @classmethod
    def create(
        cls,
        *,
        target_ro_water_l: float,
        estimated_process_volume_l: float,
        planned_nutrient_ml: float,
        max_mixer_l: float,
        prefill_target_l: float | None = None,
    ) -> "VolumePlan":
        plan = cls(
            target_ro_water_l=target_ro_water_l,
            estimated_process_volume_l=estimated_process_volume_l,
            planned_nutrient_ml=planned_nutrient_ml,
            max_mixer_l=max_mixer_l,
            prefill_target_l=prefill_target_l,
        )
        problem = check_volume_plan(plan)
        if problem is not None:
            raise VolumeModelError(problem[0], problem[1])
        return plan

    @property
    def planned_nutrient_ul(self) -> int:
        return ml_to_ul(self.planned_nutrient_ml, "planned_nutrient_ml")

    @property
    def dosing_planned(self) -> bool:
        return self.planned_nutrient_ul > 0

    @property
    def limit_l(self) -> float:
        """Wirksame Obergrenze fuer den Mixer-Gesamtfuellstand."""
        return min(self.max_mixer_l, MAX_PROCESS_VOLUME_L)


def check_volume_plan(plan: VolumePlan) -> tuple[VolumeReason, str] | None:
    """None = gueltig, sonst (Grund, Meldung). Wirft nie."""
    ro = plan.target_ro_water_l
    if not _is_number(ro) or ro <= 0:
        return VolumeReason.RO_TARGET_INVALID, f"target_ro_water_l muss > 0 und endlich sein (war: {safe_repr(ro)})."
    if ro > MAX_RECIPE_RO_WATER_L:
        return (
            VolumeReason.RO_TARGET_ABOVE_LIMIT,
            f"RO-Wassermenge {ro} l ueberschreitet das Maximum von {MAX_RECIPE_RO_WATER_L} l.",
        )

    if not _is_number(plan.estimated_process_volume_l):
        return VolumeReason.INVALID_INPUT, f"estimated_process_volume_l ungueltig (war: {safe_repr(plan.estimated_process_volume_l)})."

    try:
        planned_ul = ml_to_ul(plan.planned_nutrient_ml, "planned_nutrient_ml")
    except VolumeModelError as exc:
        return exc.reason, str(exc)

    mixer = plan.max_mixer_l
    if not _is_number(mixer) or mixer <= 0 or mixer > MAX_PROCESS_VOLUME_L:
        return (
            VolumeReason.MAX_MIXER_INVALID,
            f"max_mixer_l muss in (0, {MAX_PROCESS_VOLUME_L}] liegen (war: {safe_repr(mixer)}).",
        )

    expected_estimate = round_l(compute_base_volume_before_ro_correction_l(ro, plan.planned_nutrient_ml))
    if plan.estimated_process_volume_l != expected_estimate:
        return (
            VolumeReason.ESTIMATE_MISMATCH,
            f"estimated_process_volume_l {safe_repr(plan.estimated_process_volume_l)} passt nicht zu RO + Naehrstoffen "
            f"({safe_repr(expected_estimate)}).",
        )

    exact_total = _frac(ro) + Fraction(planned_ul, UL_PER_L)
    if exact_total > _frac(MAX_PROCESS_VOLUME_L):
        return (
            VolumeReason.PROCESS_VOLUME_ABOVE_LIMIT,
            f"Prozessvolumen ueberschreitet {MAX_PROCESS_VOLUME_L} l (RO {ro} l + {plan.planned_nutrient_ml} ml).",
        )
    if exact_total > _frac(mixer):
        return (
            VolumeReason.PROCESS_VOLUME_ABOVE_LIMIT,
            f"Prozessvolumen ueberschreitet die Mixergrenze {mixer} l.",
        )

    prefill = plan.prefill_target_l
    if prefill is None:
        if planned_ul > 0:
            return VolumeReason.PREFILL_TARGET_MISSING, "Bei geplanter Dosierung ist ein Vorfuellziel erforderlich."
    elif not _is_number(prefill) or prefill <= 0 or _frac(prefill) > _frac(ro):
        return (
            VolumeReason.PREFILL_TARGET_INVALID,
            f"prefill_target_l muss > 0 und <= RO-Sollmenge {ro} l sein (war: {safe_repr(prefill)}).",
        )
    return None


def expected_target_total_l(plan: VolumePlan) -> float:
    """Erwarteter Mixer-Gesamtzielstand eines Laufs, gemeinsame Quelle fuer die
    Startpruefung und die Laufzeitpruefung.

    Bei aktiver Dosierung das Rezept-Gesamtvolumen (RO + Naehrstoffe), sonst
    unveraendert die RO-Sollmenge (heutiges Verhalten ohne Dosierung)."""
    problem = check_volume_plan(plan)
    if problem is not None:
        raise VolumeModelError(problem[0], problem[1])
    return plan.estimated_process_volume_l if plan.dosing_planned else plan.target_ro_water_l


# --- Zustand ---------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeState:
    """Momentaufnahme der Mengenbuchfuehrung (Eingabe, keine Messung durch
    dieses Modul).

    ``mixer_measured_l``       SENSOR_MEASURED
    ``confirmed_nutrient_ml``  PROTOCOL_CONFIRMED (Summe der per DONE bestaetigten Chunks)
    ``in_flight_nutrient_ml``  UNCONFIRMED: gestartete, nicht bestaetigte Chunks (geplante ml)
    ``uncertain_nutrient_ml``  UNCONFIRMED: abgebrochene/unsichere Chunks (obere Grenze)
    """

    mixer_measured_l: float
    confirmed_nutrient_ml: float = 0.0
    in_flight_nutrient_ml: float = 0.0
    uncertain_nutrient_ml: float = 0.0

    @classmethod
    def from_ul(
        cls, *, mixer_measured_l: float, confirmed_ul: int = 0, in_flight_ul: int = 0, uncertain_ul: int = 0
    ) -> "VolumeState":
        for name, value in (("confirmed_ul", confirmed_ul), ("in_flight_ul", in_flight_ul), ("uncertain_ul", uncertain_ul)):
            if type(value) is not int or value < 0:
                raise VolumeModelError(VolumeReason.INVALID_INPUT, f"{name} muss eine nichtnegative Ganzzahl sein (war: {safe_repr(value)}).")
        return cls(
            mixer_measured_l=mixer_measured_l,
            confirmed_nutrient_ml=ul_to_ml(confirmed_ul),
            in_flight_nutrient_ml=ul_to_ml(in_flight_ul),
            uncertain_nutrient_ml=ul_to_ml(uncertain_ul),
        )


VOLUME_FIELD_EVIDENCE: dict[str, Evidence] = {
    "target_ro_water_l": Evidence.PLANNED,
    "estimated_process_volume_l": Evidence.PLANNED,
    "planned_nutrient_ml": Evidence.PLANNED,
    "prefill_target_l": Evidence.PLANNED,
    "phase_target_l": Evidence.PLANNED,
    "confirmed_nutrient_ml": Evidence.PROTOCOL_CONFIRMED,
    "mixer_measured_l": Evidence.SENSOR_MEASURED,
    "in_flight_nutrient_ml": Evidence.UNCONFIRMED,
    "uncertain_nutrient_ml": Evidence.UNCONFIRMED,
    "remaining_capacity_l": Evidence.PLANNED,
    "indirect_ro_estimate_l": Evidence.UNCONFIRMED,
}

RO_AMOUNT_BASIS = "indirect_level_minus_confirmed"


@dataclass(frozen=True)
class _Accounting:
    planned_ul: int
    confirmed_ul: int
    in_flight_ul: int
    uncertain_ul: int

    @property
    def not_started_ul(self) -> int:
        return self.planned_ul - self.confirmed_ul - self.in_flight_ul - self.uncertain_ul

    @property
    def still_possible_ul(self) -> int:
        """Konservativ: alles, was noch nicht per DONE bestaetigt ist, kann
        noch im Tank ankommen bzw. ist (anteilig) schon im Messwert enthalten
        - beides zaehlt hier voll als noch ausstehend (Doppelzaehlung ist
        gewollt, siehe Modulbeschreibung/Grenzen)."""
        return self.planned_ul - self.confirmed_ul


def _account(plan: VolumePlan, state: VolumeState) -> _Accounting | tuple[VolumeReason, str]:
    planned_ul = ml_to_ul(plan.planned_nutrient_ml, "planned_nutrient_ml")
    parts: dict[str, int] = {}
    for name in ("confirmed_nutrient_ml", "in_flight_nutrient_ml", "uncertain_nutrient_ml"):
        try:
            parts[name] = ml_to_ul(getattr(state, name), name)
        except VolumeModelError as exc:
            return exc.reason, str(exc)
    if parts["confirmed_nutrient_ml"] > planned_ul:
        return (
            VolumeReason.CONFIRMED_EXCEEDS_PLANNED,
            f"Bestaetigte Menge {state.confirmed_nutrient_ml} ml ueberschreitet die geplante Menge {plan.planned_nutrient_ml} ml.",
        )
    if sum(parts.values()) > planned_ul:
        return (
            VolumeReason.NUTRIENT_ACCOUNTING_INCONSISTENT,
            "Bestaetigt + in flight + unsicher uebersteigt die geplante Menge (moegliche Doppelbuchung).",
        )
    return _Accounting(
        planned_ul=planned_ul,
        confirmed_ul=parts["confirmed_nutrient_ml"],
        in_flight_ul=parts["in_flight_nutrient_ml"],
        uncertain_ul=parts["uncertain_nutrient_ml"],
    )


@dataclass(frozen=True)
class VolumeEvaluation:
    """Ergebnis einer Bewertung. Bei ``ok=False`` sind ``reason``/``message``
    gesetzt und die Zahlenfelder ``None``."""

    ok: bool
    reason: VolumeReason | None = None
    message: str | None = None

    planned_nutrient_ml: float | None = None
    confirmed_nutrient_ml: float | None = None
    in_flight_nutrient_ml: float | None = None
    uncertain_nutrient_ml: float | None = None
    not_started_nutrient_ml: float | None = None
    still_possible_nutrient_ml: float | None = None
    mixer_measured_l: float | None = None
    phase_target_l: float | None = None
    target_reached: bool | None = None
    # Exakt in uL, vorzeichenbehaftet (negativ = Kapazitaet verletzt); die
    # verbindliche Groesse. ``remaining_capacity_l`` ist deren Anzeigewert.
    remaining_capacity_ul: int | None = None
    remaining_capacity_l: float | None = None
    # Indirekte Schaetzung, KEIN Nachweis, nie als Grenzwert verwendet.
    indirect_ro_estimate_l: float | None = None


def _fail(reason: VolumeReason, message: str) -> VolumeEvaluation:
    return VolumeEvaluation(ok=False, reason=reason, message=message)


def evaluate_volume_state(
    plan: VolumePlan, state: VolumeState, *, phase_target_l: float | None = None
) -> VolumeEvaluation:
    """Bewertet Plan + Zustand. Wirft nie; ein ungueltiger Zustand liefert
    ``ok=False`` mit Grund.

    Pruefreihenfolge: Plan, Eingabetypen/Vorzeichen, Mengenbuchfuehrung,
    Mixergrenze, Kapazitaet, Phasenziel.

    Kapazitaetsformel (exakt, konservativ)::

        remaining_capacity = limit_l - mixer_measured_l - (planned - confirmed)

    ``planned - confirmed`` enthaelt in-flight, unsichere und noch nicht
    gestartete Mengen vollstaendig. Ein in-flight/unsicherer Anteil kann
    bereits im Mixer-Messwert stecken und wird dann doppelt gezaehlt: die
    Formel UNTERschaetzt die Kapazitaet, ueberschaetzt sie nie. Grenze: sie
    sagt nichts ueber die RO-Menge, die noch bis zum Phasenziel gefoerdert
    wird (die ist durch das Rezept-Gesamtvolumen <= 185 l begrenzt).
    """
    problem = check_volume_plan(plan)
    if problem is not None:
        return _fail(*problem)

    if not _is_number(state.mixer_measured_l):
        return _fail(VolumeReason.INVALID_INPUT, f"mixer_measured_l muss eine endliche Zahl sein (war: {safe_repr(state.mixer_measured_l)}).")
    if state.mixer_measured_l < 0:
        return _fail(VolumeReason.NEGATIVE_VALUE, f"mixer_measured_l darf nicht negativ sein (war: {safe_repr(state.mixer_measured_l)}).")

    accounting = _account(plan, state)
    if isinstance(accounting, tuple):
        return _fail(*accounting)

    mixer = _frac(state.mixer_measured_l)
    limit = _frac(plan.limit_l)
    if mixer > limit:
        return _fail(
            VolumeReason.MIXER_ABOVE_LIMIT,
            f"Mixerfuellstand {state.mixer_measured_l} l ueberschreitet die Grenze {plan.limit_l} l.",
        )

    margin_ul = math.floor((limit - mixer - Fraction(accounting.still_possible_ul, UL_PER_L)) * UL_PER_L)
    if margin_ul < 0:
        return _fail(
            VolumeReason.CAPACITY_EXCEEDED,
            f"Mixer {state.mixer_measured_l} l + noch nicht bestaetigte Menge "
            f"{ul_to_ml(accounting.still_possible_ul)} ml ueberschreitet {plan.limit_l} l.",
        )

    target_reached: bool | None = None
    if phase_target_l is not None:
        if not _is_number(phase_target_l) or phase_target_l <= 0 or _frac(phase_target_l) > _frac(plan.estimated_process_volume_l):
            return _fail(
                VolumeReason.PHASE_TARGET_INVALID,
                f"phase_target_l muss > 0 und <= Rezept-Gesamtvolumen {plan.estimated_process_volume_l} l sein "
                f"(war: {safe_repr(phase_target_l)}).",
            )
        target_reached = mixer >= _frac(phase_target_l)

    return VolumeEvaluation(
        ok=True,
        planned_nutrient_ml=ul_to_ml(accounting.planned_ul),
        confirmed_nutrient_ml=ul_to_ml(accounting.confirmed_ul),
        in_flight_nutrient_ml=ul_to_ml(accounting.in_flight_ul),
        uncertain_nutrient_ml=ul_to_ml(accounting.uncertain_ul),
        not_started_nutrient_ml=ul_to_ml(accounting.not_started_ul),
        still_possible_nutrient_ml=ul_to_ml(accounting.still_possible_ul),
        mixer_measured_l=state.mixer_measured_l,
        phase_target_l=phase_target_l,
        target_reached=target_reached,
        remaining_capacity_ul=margin_ul,
        remaining_capacity_l=margin_ul / UL_PER_L,
        indirect_ro_estimate_l=float(mixer - Fraction(accounting.confirmed_ul, UL_PER_L)),
    )


# --- Phasenziele -------------------------------------------------------------------


def prefill_phase_target_l(plan: VolumePlan) -> float:
    """Ziel der ersten RO-Phase (reines RO-Wasser, noch keine Dosierung)."""
    problem = check_volume_plan(plan)
    if problem is not None:
        raise VolumeModelError(problem[0], problem[1])
    if plan.prefill_target_l is None:
        raise VolumeModelError(VolumeReason.PREFILL_TARGET_MISSING, "Kein Vorfuellziel im Plan.")
    return plan.prefill_target_l


def dynamic_phase_target_l(plan: VolumePlan, confirmed_nutrient_ml: float) -> float:
    """Aktuelles Ziel der zweiten RO-Phase: RO-Sollmenge + per DONE
    bestaetigte Naehrstoffmenge, nach den Rundungsregeln der Rezeptdomaene
    (``round_l``). Steigt monoton mit der bestaetigten Menge; bei vollstaendig
    bestaetigter Dosierung ist es exakt ``estimated_process_volume_l``.

    Rundungsabweichung (verbindliche Rezept-Rundung, hier NICHT geaendert):
    ``round_l`` rundet auf 3 Nachkommastellen (1 ml). Das Ergebnis weicht vom
    exakten Wert ``RO + bestaetigt`` um hoechstens 0.0005 l = 0.5 ml ab, in
    beide Richtungen (zzgl. Gleitkomma-Darstellungsrauschen ~1e-13 l):

    * nach UNTEN gerundet: die RO-Pumpe wird bis zu 0.5 ml frueher
      abgeschaltet als beim exakten Ziel; eine Schlusspruefung gegen dieses
      Ziel ist dann bis zu 0.5 ml laxer.
    * nach OBEN gerundet: bis zu 0.5 ml strenger.

    Bei Rezepten mit RO-Sollmenge mit mehr als 3 Nachkommastellen (der
    Rohwert wird nicht gerundet) gilt dieselbe Grenze zusaetzlich gegenueber
    dem Roh-RO-Wert. Ein Pegelsensor, der 0.5 ml aufloest, ist nicht belegt;
    die Abweichung liegt voraussichtlich unterhalb der Sensorgenauigkeit.
    Folgen fuer die State-Machine-Integration (Stufe 4): Ziel- und
    Schlusspruefung duerfen nicht strenger als diese Rundung formuliert und
    nicht als Mengennachweis (< 1 ml) interpretiert werden; der Vergleich
    ``Mixer >= Ziel`` bleibt exakt, es wird keine zusaetzliche Toleranz
    eingefuehrt. Der Unterschied zum Ohne-Dosierung-Ziel
    (``expected_target_total_l`` liefert dort den ungerundeten RO-Rohwert)
    betraegt ebenfalls hoechstens 0.5 ml."""
    problem = check_volume_plan(plan)
    if problem is not None:
        raise VolumeModelError(problem[0], problem[1])
    confirmed_ul = ml_to_ul(confirmed_nutrient_ml, "confirmed_nutrient_ml")
    if confirmed_ul > plan.planned_nutrient_ul:
        raise VolumeModelError(
            VolumeReason.CONFIRMED_EXCEEDS_PLANNED,
            f"Bestaetigte Menge {confirmed_nutrient_ml} ml ueberschreitet die geplante Menge.",
        )
    return round_l(compute_base_volume_before_ro_correction_l(plan.target_ro_water_l, ul_to_ml(confirmed_ul)))


@dataclass(frozen=True)
class FrozenPhaseTarget:
    """Beim Abschalten der RO-Pumpe eingefrorenes Ziel. Waehrend der
    Bestaetigung wird ausschliesslich dagegen verglichen, nie gegen das
    inzwischen weiter gestiegene dynamische Ziel."""

    target_l: float
    confirmed_nutrient_ml_at_freeze: float


def freeze_phase_target(plan: VolumePlan, confirmed_nutrient_ml: float) -> FrozenPhaseTarget:
    target = dynamic_phase_target_l(plan, confirmed_nutrient_ml)
    return FrozenPhaseTarget(
        target_l=target,
        confirmed_nutrient_ml_at_freeze=ul_to_ml(ml_to_ul(confirmed_nutrient_ml, "confirmed_nutrient_ml")),
    )


@dataclass(frozen=True)
class TargetConfirmation:
    """``confirmable`` ist nur dann True, wenn ALLE vier Bedingungen gelten:
    gueltiger Volumenzustand, eingefrorener Zielstand erreicht, keine Dosis
    mehr in flight (quiescent) und keine unsichere Naehrstoffmenge.
    ``reached=False`` bei gueltigem Zustand ist ein Fachfehler-Kandidat
    (Zielstand nicht bestaetigt), kein Eingabefehler. Unsichere Mengen liefern
    ``ok=False`` mit ``UNCERTAIN_NUTRIENT_PRESENT`` (``reached``/``quiescent``
    bleiben zur Diagnose gesetzt)."""

    ok: bool
    reason: VolumeReason | None
    message: str | None
    reached: bool | None
    quiescent: bool | None
    confirmable: bool
    target_l: float | None
    evaluation: VolumeEvaluation | None


def evaluate_target_confirmation(plan: VolumePlan, frozen: FrozenPhaseTarget, state: VolumeState) -> TargetConfirmation:
    def bad(reason: VolumeReason, message: str, evaluation: VolumeEvaluation | None = None) -> TargetConfirmation:
        return TargetConfirmation(False, reason, message, None, None, False, None, evaluation)

    if not isinstance(frozen, FrozenPhaseTarget):
        return bad(VolumeReason.INVALID_INPUT, "frozen muss ein FrozenPhaseTarget sein.")
    try:
        frozen_ul = ml_to_ul(frozen.confirmed_nutrient_ml_at_freeze, "confirmed_nutrient_ml_at_freeze")
        expected = dynamic_phase_target_l(plan, ul_to_ml(frozen_ul))
    except VolumeModelError as exc:
        return bad(exc.reason, str(exc))
    if not _is_number(frozen.target_l) or frozen.target_l != expected:
        return bad(
            VolumeReason.FROZEN_TARGET_MISMATCH,
            f"Eingefrorenes Ziel {safe_repr(frozen.target_l)} passt nicht zur bestaetigten Menge beim Einfrieren ({safe_repr(expected)}).",
        )

    evaluation = evaluate_volume_state(plan, state, phase_target_l=frozen.target_l)
    if not evaluation.ok:
        return bad(evaluation.reason, evaluation.message or "", evaluation)  # type: ignore[arg-type]

    if ml_to_ul(state.confirmed_nutrient_ml, "confirmed_nutrient_ml") < frozen_ul:
        return bad(
            VolumeReason.CONFIRMED_DECREASED,
            "Bestaetigte Naehrstoffmenge ist seit dem Einfrieren des Ziels gesunken.",
            evaluation,
        )

    quiescent = ml_to_ul(state.in_flight_nutrient_ml, "in_flight_nutrient_ml") == 0
    reached = bool(evaluation.target_reached)

    # Eine unsichere (abgebrochene) Naehrstoffmenge heisst: die Buchfuehrung ist
    # nicht belastbar. Das ist kein Warten (wie in flight), sondern ein Sperrgrund -
    # niemals bestaetigbar, auch wenn der Zielstand erreicht ist.
    if ml_to_ul(state.uncertain_nutrient_ml, "uncertain_nutrient_ml") > 0:
        return TargetConfirmation(
            False,
            VolumeReason.UNCERTAIN_NUTRIENT_PRESENT,
            "Unsichere Naehrstoffmenge vorhanden - Zielbestaetigung gesperrt.",
            reached,
            quiescent,
            False,
            frozen.target_l,
            evaluation,
        )
    return TargetConfirmation(True, None, None, reached, quiescent, reached and quiescent, frozen.target_l, evaluation)


# --- Abschluss und Wiederaufnahme ------------------------------------------------------


def evaluate_final_volume(plan: VolumePlan, state: VolumeState) -> VolumeEvaluation:
    """Schlusspruefung nach Dosierende: alles bestaetigt, nichts in flight oder
    unsicher, Mixerstand mindestens RO-Soll + bestaetigte Menge. Ein zu
    niedriger Stand ist ein Widerspruch (die RO-Pumpe wird nicht erneut
    eingeschaltet), kein Anlass fuer eine Korrektur."""
    target = None
    try:
        target = dynamic_phase_target_l(plan, state.confirmed_nutrient_ml)
    except VolumeModelError as exc:
        return _fail(exc.reason, str(exc))
    evaluation = evaluate_volume_state(plan, state, phase_target_l=target)
    if not evaluation.ok:
        return evaluation
    if evaluation.confirmed_nutrient_ml != evaluation.planned_nutrient_ml or (
        evaluation.in_flight_nutrient_ml or evaluation.uncertain_nutrient_ml
    ):
        return _fail(VolumeReason.DOSING_INCOMPLETE, "Dosierung nicht vollstaendig bestaetigt oder noch Mengen offen.")
    if not evaluation.target_reached:
        return _fail(
            VolumeReason.FINAL_LEVEL_BELOW_TARGET,
            f"Mixerfuellstand {state.mixer_measured_l} l liegt unter RO-Soll + bestaetigter Menge ({target} l).",
        )
    return evaluation


def dosing_progress(plan: VolumePlan, state: VolumeState) -> float | None:
    """Anteil bestaetigt/geplant in [0, 1]; None wenn keine Dosierung geplant
    oder der Zustand ungueltig ist. Nur per DONE bestaetigte Menge zaehlt."""
    evaluation = evaluate_volume_state(plan, state)
    if not evaluation.ok or not plan.dosing_planned:
        return None
    return ml_to_ul(state.confirmed_nutrient_ml) / plan.planned_nutrient_ul


@dataclass(frozen=True)
class RoResumeDecision:
    allowed: bool
    reason: VolumeReason | None
    message: str | None
    progress: float | None
    evaluation: VolumeEvaluation | None


def evaluate_ro_resume(
    plan: VolumePlan,
    state: VolumeState,
    *,
    progress_threshold: float,
    phase_baseline_l: float,
    max_negative_drift_l: float,
    ro_on_count: int,
) -> RoResumeDecision:
    """Rein rechnerischer Teil der Bedingungen fuer die einmalige
    Wiederaufnahme der RO-Fuellung. Weitere Bedingungen (frische Messung,
    Koordinator gesund, Zirkulation) pruefen State Machine/Koordinator.

    ``progress_threshold`` (vorlaeufig), ``phase_baseline_l`` und
    ``max_negative_drift_l`` kommen vom Aufrufer; es gibt keine Defaults."""

    def no(reason: VolumeReason, message: str, progress: float | None = None, evaluation: VolumeEvaluation | None = None) -> RoResumeDecision:
        return RoResumeDecision(False, reason, message, progress, evaluation)

    if type(ro_on_count) is not int:
        return no(VolumeReason.INVALID_INPUT, f"ro_on_count muss eine Ganzzahl sein (war: {safe_repr(ro_on_count)}).")
    for name, value in (("progress_threshold", progress_threshold), ("phase_baseline_l", phase_baseline_l), ("max_negative_drift_l", max_negative_drift_l)):
        if not _is_number(value):
            return no(VolumeReason.INVALID_INPUT, f"{name} muss eine endliche Zahl sein (war: {safe_repr(value)}).")
    if not 0 < progress_threshold <= 1:
        return no(VolumeReason.INVALID_INPUT, f"progress_threshold muss in (0, 1] liegen (war: {safe_repr(progress_threshold)}).")
    if phase_baseline_l < 0 or max_negative_drift_l < 0:
        return no(VolumeReason.NEGATIVE_VALUE, "phase_baseline_l und max_negative_drift_l duerfen nicht negativ sein.")

    evaluation = evaluate_volume_state(plan, state)
    if not evaluation.ok:
        return no(evaluation.reason, evaluation.message or "", None, evaluation)  # type: ignore[arg-type]
    if not plan.dosing_planned:
        return no(VolumeReason.NO_DOSING_PLANNED, "Keine Dosierung geplant.", None, evaluation)
    if ro_on_count != 1:
        return no(VolumeReason.RESUME_RO_ON_COUNT_INVALID, f"Wiederaufnahme nur nach genau einer Einschaltung (war: {safe_repr(ro_on_count)}).", None, evaluation)
    if ml_to_ul(state.uncertain_nutrient_ml) > 0:
        return no(VolumeReason.RESUME_UNCERTAIN_NUTRIENT, "Unsichere Naehrstoffmenge vorhanden - keine Wiederaufnahme.", None, evaluation)

    progress = ml_to_ul(state.confirmed_nutrient_ml) / plan.planned_nutrient_ul
    if Fraction(ml_to_ul(state.confirmed_nutrient_ml), plan.planned_nutrient_ul) < _frac(progress_threshold):
        return no(VolumeReason.RESUME_PROGRESS_BELOW_THRESHOLD, f"Fortschritt {progress:.4f} unter Schwelle {progress_threshold}.", progress, evaluation)
    if _frac(state.mixer_measured_l) < _frac(phase_baseline_l) - _frac(max_negative_drift_l):
        return no(VolumeReason.RESUME_LEVEL_BELOW_BASELINE, "Mixerfuellstand liegt unter Phasenbeginn abzueglich erlaubter Drift.", progress, evaluation)

    return RoResumeDecision(True, None, None, progress, evaluation)
