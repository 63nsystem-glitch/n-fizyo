"""Geldbeträge in Franken – EIN Helfer für alle Module (Abrechnung, Kasse, Firmenkunden, Export).

Warum nicht round(x, 2): float rundet binär. round(2.675, 2) == 2.67 und round(1.5 * 180.35, 2) == 270.52, kaufmännisch
richtig ist 2.68 und 270.53. Hier wird mit Decimal gerechnet (jede Zahl über ihre kürzeste Schreibweise str(x)) und
kaufmännisch gerundet (ROUND_HALF_UP, «ab 5 aufrunden», auch bei negativen Beträgen vom Betrag her).

  rp(x)              auf Rappen (0.01)                         rp(2.675) → 2.68
  times(a, b, …)     Produkt, erst danach auf Rappen           times(1.5, 180.35) → 270.53 (Anzahl × Preis, TP × TPW × Faktor)
  total(werte)       Summe auf Rappen
  vat_in(brutto, s)  MWST-Anteil eines Endpreises mit Satz s   vat_in(108.10, 8.1) → 8.10
  cash(x)            Bargeld auf 5 Rappen (Schweizer Regel)    cash(186.48) → 186.50, cash(186.47) → 186.45, cash(0.025) → 0.05
  cash_settle(o, b)  Barzahlung b auf offenen Betrag o: (gutgeschrieben, Bargeld, Rundung) – siehe dort
  parse(text)        Betrag aus einem Formular (1'290.50, 12,5) – None bei Unsinn (leer, zu lang, unendlich, ausserhalb)

Bargeld: In der Schweiz gibt es keine 1- und 2-Rappen-Münzen. Bar wird auf 5 Rappen gerundet; Bank, QR-Rechnung, Twint und
Karte bleiben auf den Rappen genau. Die Rundung wird nie in den Rechnungsbetrag eingerechnet, sondern als eigene Zahl geführt
(Quittung, Zahlung, Kassenabschluss, Buchungsjournal «Rundungsdifferenzen»).
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable

CENT = Decimal("0.01")
CASH_STEP = 0.05
CASH_TOLERANCE = 0.05          # Restbetrag unter 5 Rappen kann bar nicht bezahlt werden → als Rundung ausgebucht
MAX_AMOUNT = 1_000_000_000.0   # oberhalb ist jede Eingabe ein Tippfehler (und float verliert Rappen)


def _d(value) -> Decimal:
    if isinstance(value, Decimal):
        d = value
    else:
        f = float(value)
        if not math.isfinite(f):
            raise ValueError(f"Kein Geldbetrag: {value!r}")
        d = Decimal(str(f))
    if not d.is_finite():
        raise ValueError(f"Kein Geldbetrag: {value!r}")
    return d


def _f(d: Decimal) -> float:
    out = float(d.quantize(CENT, rounding=ROUND_HALF_UP))
    return 0.0 if out == 0 else out          # kein «-0.0»


def rp(value) -> float:
    """Auf Rappen runden, kaufmännisch (ROUND_HALF_UP)."""
    return _f(_d(value))


def times(*factors) -> float:
    """Produkt der Faktoren (in Decimal gerechnet), dann auf Rappen: Anzahl × Preis, Anzahl × TP × TPW × Faktor."""
    d = Decimal(1)
    for x in factors:
        d *= _d(x)
    return _f(d)


def total(values: Iterable) -> float:
    """Summe auf Rappen."""
    return _f(sum((_d(v) for v in values), Decimal(0)))


def vat_in(gross, rate) -> float:
    """MWST-Anteil eines Endpreises (inkl. MWST) beim Satz «rate» in Prozent."""
    r = _d(rate)
    if not r:
        return 0.0
    return _f(_d(gross) * r / (Decimal(100) + r))


def cash(value) -> float:
    """Bargeld auf 5 Rappen runden (kaufmännisch: .x25 → .x5, .x75 → .x0 + 0.10)."""
    d = (_d(value) * 20).quantize(Decimal(1), rounding=ROUND_HALF_UP) / 20
    return _f(d)


def is_cash_amount(value) -> bool:
    """Lässt sich der Betrag bar (in 5-Rappen-Schritten) bezahlen?"""
    return cash(value) == rp(value)


def cash_settle(open_amount, given) -> tuple[float, float, float]:
    """Barzahlung «given» auf einen offenen Betrag. Gibt (gutgeschrieben, Bargeld, Rundung) zurück, Bargeld = gutgeschrieben + Rundung.

    - Bargeld = given auf 5 Rappen gerundet.
    - Liegt das Bargeld weniger als 5 Rappen neben dem offenen Betrag, ist die Rechnung bezahlt: gutgeschrieben = offener Betrag,
      die Differenz (höchstens ±0.04) ist Rundung. So bleiben nie ein paar Rappen offen (keine Mahnung für 3 Rappen).
    - Sonst (Teilzahlung oder deutlich mehr): gutgeschrieben = Bargeld, keine Rundung.
    Negative Beträge (Rückzahlung bar) gelten spiegelbildlich."""
    o = rp(open_amount)
    c = cash(given)
    if c and o and (o > 0) == (c > 0) and abs(c - o) < CASH_TOLERANCE - 1e-9:
        return o, c, rp(c - o)
    return c, c, 0.0


def parse(text, *, lo: float = 0.0, hi: float = MAX_AMOUNT, max_len: int = 20) -> float | None:
    """Betrag aus einem Formularfeld: «1'290.50», «1’290,50», «12.5». None bei leer, zu lang, keine Zahl, unendlich/NaN
    oder ausserhalb [lo, hi] – nie eine Ausnahme (Formulare zeigen dann einen Fehler statt «500»)."""
    s = str(text if text is not None else "").strip().replace("’", "").replace("'", "").replace(" ", "").replace(",", ".")
    if not s or len(s) > max_len:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if not math.isfinite(f) or not (lo <= f <= hi):
        return None
    try:
        return rp(f)
    except (ValueError, InvalidOperation):
        return None
