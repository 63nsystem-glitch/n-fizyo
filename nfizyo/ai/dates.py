"""Datumsangaben in deutschen und französischen Texten erkennen."""
from __future__ import annotations

import re
from datetime import date, timedelta

MONTHS = {
    # deutsch
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
    "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    # französisch
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))

NUMERIC = re.compile(r"(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})(?![\d])")
WORDS = re.compile(rf"(?<![\d])(\d{{1,2}})(?:\.|er)?\s+({_MONTH_NAMES})\s+(\d{{4}})", re.IGNORECASE)
ANY_DATE = re.compile(NUMERIC.pattern + "|" + WORDS.pattern, re.IGNORECASE)

WEEKS = re.compile(r"(\d{1,2})\s*(?:-|–|bis|à)?\s*(\d{1,2})?\s*(wochen|woche|wo\.|semaines?|sem\.)", re.IGNORECASE)
DAYS = re.compile(r"(\d{1,3})\s*(tage|tagen|jours)", re.IGNORECASE)
MONTHS_DURATION = re.compile(r"(\d{1,2})\s*(monate|monaten|mois)", re.IGNORECASE)
UNTIL = re.compile(
    r"(?:bis(?: zum| am)?|jusqu'au|jusqu’au|jusqu'à|jusqu’à)\s+(" + NUMERIC.pattern + "|" + WORDS.pattern + ")",
    re.IGNORECASE,
)


def _mk(d: int, m: int, y: int) -> date | None:
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_first(text: str) -> date | None:
    m = ANY_DATE.search(text)
    return _from_match(m) if m else None


def _from_match(m: re.Match) -> date | None:
    g = m.groups()
    if g[0]:
        return _mk(int(g[0]), int(g[1]), int(g[2]))
    month = MONTHS.get(g[4].lower())
    return _mk(int(g[3]), month, int(g[5])) if month else None


def all_dates(text: str) -> list[tuple[int, date]]:
    out = []
    for m in ANY_DATE.finditer(text):
        d = _from_match(m)
        if d:
            out.append((m.start(), d))
    return out


def duration_end(text: str, base: date) -> date | None:
    """Ende einer Frist: "für 6 Wochen" → base + 6 Wochen; "bis zum 13.11.2026" → Datum.
    Bei Spannen ("4–6 Wochen") gilt der längere Wert (vorsichtig)."""
    m = UNTIL.search(text)
    if m:
        d = parse_first(m.group(1))
        if d:
            return d
    m = WEEKS.search(text)
    if m:
        weeks = max(int(m.group(1)), int(m.group(2) or 0))
        return base + timedelta(weeks=weeks)
    m = DAYS.search(text)
    if m:
        return base + timedelta(days=int(m.group(1)))
    m = MONTHS_DURATION.search(text)
    if m:
        return base + timedelta(days=30 * int(m.group(1)))
    return None
