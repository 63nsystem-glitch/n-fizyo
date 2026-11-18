"""Vorschläge zur Steigerung im Krafttraining – einfache, nachvollziehbare Regeln (keine KI).

Die App schlägt nur vor; die Therapeutin / der Therapeut übernimmt oder verwirft. Grundlage sind die letzten zwei
Trainingstage an einem Gerät:
- Schmerz 5 oder mehr im letzten Training → leichter (−15 %), Grund «pain».
- Schmerz deutlich gestiegen (≥ 2 Punkte, mindestens 3) → Gewicht halten, Grund «pain_rising».
- Zu schwer: Anstrengung 9–10 oder deutlich weniger Wiederholungen (2 Trainings) → leichter (−10 %), Grund «too_hard».
- Zu leicht: an beiden Tagen alle Sätze mit allen Wiederholungen, Anstrengung höchstens 6, Schmerz höchstens 3
  → schwerer (+5 %, mindestens eine Gewichtsstufe), Grund «easy».
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

PAIN_STOP = 5
EFFORT_EASY = 6
EFFORT_HARD = 9
PAIN_OK = 3


@dataclass
class Day:
    """Sätze eines Trainingstags an einem Gerät."""
    reps: list[int | None] = field(default_factory=list)
    loads: list[float | None] = field(default_factory=list)
    effort: list[int | None] = field(default_factory=list)
    pain: list[int | None] = field(default_factory=list)

    def max_of(self, values) -> int | None:
        v = [x for x in values if x is not None]
        return max(v) if v else None


@dataclass
class Suggestion:
    kind: str           # increase | decrease | hold
    reason: str         # easy | too_hard | pain | pain_rising
    from_load: float | None
    to_load: float | None


def round_to(value: float, step: float, up: bool) -> float:
    step = step or 1.0
    n = value / step
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * step, 2)


def suggest(*, sets: int | None, reps: int | None, load: float | None, step: float, days: list[Day]) -> Suggestion | None:
    """days: Trainingstage mit Sätzen, ältester zuerst (nur die letzten zwei zählen)."""
    if not days or not load:
        return None
    last = days[-1]
    prev = days[-2] if len(days) > 1 else None
    pain_last = last.max_of(last.pain)
    if pain_last is not None and pain_last >= PAIN_STOP:
        return Suggestion("decrease", "pain", load, max(step, round_to(load * 0.85, step, up=False)))
    if prev is not None and pain_last is not None:
        pain_prev = prev.max_of(prev.pain) or 0
        if pain_last >= 3 and pain_last - pain_prev >= 2:
            return Suggestion("hold", "pain_rising", load, load)
    if prev is None:
        return None
    pair = (prev, last)

    def too_hard(d: Day) -> bool:
        eff = d.max_of(d.effort)
        short = reps is not None and any(r is not None and r <= reps - 3 for r in d.reps)
        return (eff is not None and eff >= EFFORT_HARD) or short

    def easy(d: Day) -> bool:
        if sets and len([r for r in d.reps if r is not None]) < sets:
            return False
        if reps and any(r is None or r < reps for r in d.reps):
            return False
        if any(x is not None and x < load for x in d.loads):
            return False
        eff, pain = d.max_of(d.effort), d.max_of(d.pain)
        return eff is not None and eff <= EFFORT_EASY and (pain is None or pain <= PAIN_OK)

    if all(too_hard(d) for d in pair):
        return Suggestion("decrease", "too_hard", load, max(step, round_to(load * 0.9, step, up=False)))
    if all(easy(d) for d in pair):
        return Suggestion("increase", "easy", load, round_to(load + max(step, load * 0.05), step, up=True))
    return None
