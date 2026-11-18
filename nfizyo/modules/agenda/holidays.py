"""Gesetzliche Feiertage pro Kanton (ohne Datenbank, testbar).

Quelle der Tabelle: Bundesamt für Justiz (BJ), «Gesetzliche Feiertage und ihnen gleichgestellte Tage in der Schweiz»
(Liste nach Art. 11 des Europäischen Übereinkommens über die Berechnung von Fristen, SR 0.221.122.3), Stand 1. Januar 2011:
https://www.bj.admin.ch/dam/bj/de/data/publiservice/service/zivilprozessrecht/kant-feiertage.pdf  (abgerufen 03.11.2026)

- Spalte a = gesetzlich anerkannte Feiertage, Spalte b = Tage, die wie gesetzliche Feiertage behandelt werden.
  Beide gelten in N-Fizyo als Feiertag (keine Online-Termine). Der Bundesfeiertag (1. August) gilt überall.
- Fussnoten der Liste, die hier umgesetzt sind:
  1/7/9  UR, AR, AI: «Der Stephanstag wird nicht gefeiert, wenn der Weihnachtstag auf einen Montag oder Freitag fällt.»
  2      FR: Fronleichnam, Mariä Himmelfahrt, Allerheiligen, Mariä Empfängnis – ausgenommen einzelne Gemeinden des Seebezirks.
  3/6    SO: Fronleichnam, Mariä Himmelfahrt, Allerheiligen – im Bezirk Bucheggberg wie Feiertag behandelt (also überall frei).
  4      SO: Ostermontag, Pfingstmontag, Stephanstag – wie Feiertag behandelt (in einzelnen Gemeinden gesetzlich).
  5      SO: «Ab 12.00 Uhr gilt der 1. Mai als gesetzlicher Feiertag.»
  8      AI: Mauritiustag (22.9.) «nur im inneren Landesteil».
  10     NE: «Le 2 janvier et le 26 décembre seulement lorsque le 1er janvier et le 25 décembre sont des dimanches.»
  SO Josefstag nur in einzelnen Gemeinden, NE Fête-Dieu nur in Le Landeron → in der Liste, aber standardmässig aus.
  Örtliche Feiertage (z. B. Patrozinien in SO) stehen nicht in der Liste – die Praxis trägt sie als Schliesstag ein.
- Bewegliche Daten: Ostern nach dem gregorianischen Kalender (Algorithmus nach Meeus/Jones/Butcher);
  Karfreitag = Ostern − 2, Ostermontag + 1, Auffahrt + 39, Pfingstmontag + 50, Fronleichnam + 60.
- Fahrtsfest GL (Näfelser Fahrt): erster Donnerstag im April; fällt er in die Karwoche, eine Woche später
  (Kanton Glarus, «Feiertagsregelung 2026»: 9. April 2026; Südostschweiz zur Fahrt 2023: 13. April).
- Jeûne genevois: «fixé au jeudi qui suit le premier dimanche du mois de septembre» (Loi sur les jours fériés, rs/GE J 1 45).
- Lundi du Jeûne fédéral (VD): Montag nach dem eidgenössischen Dank-, Buss- und Bettag (dritter Sonntag im September).

Die Praxis kann jeden Tag einzeln aus- oder einschalten (Leitung › Einstellungen › Feiertage), weil Gemeinden abweichen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

CANTONS = ("AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GL", "GR", "JU", "LU", "NE", "NW", "OW", "SG", "SH", "SO", "SZ",
           "TG", "TI", "UR", "VD", "VS", "ZG", "ZH")
DEFAULT_CANTON = "BE"

_FIXED = {
    "neujahr": (1, 1), "berchtold": (1, 2), "dreikoenige": (1, 6), "republique_ne": (3, 1), "josef": (3, 19), "mai": (5, 1),
    "plebiscite_ju": (6, 23), "peterpaul": (6, 29), "bundesfeier": (8, 1), "himmelfahrt": (8, 15), "mauritius": (9, 22),
    "bruderklaus": (9, 25), "allerheiligen": (11, 1), "empfaengnis": (12, 8), "weihnachten": (12, 25), "stephan": (12, 26),
    "restauration_ge": (12, 31),
}
_EASTER_OFFSET = {"karfreitag": -2, "ostermontag": 1, "auffahrt": 39, "pfingstmontag": 50, "fronleichnam": 60}

# Besonderheiten (Fussnoten der BJ-Liste)
XMAS = "xmas"            # Stephanstag entfällt, wenn Weihnachten auf Montag oder Freitag fällt (UR, AR, AI)
AFTER_SUNDAY = "sunday"  # nur wenn der Vortag ein Sonntag ist (NE: 2.1. und 26.12.)
NOON = "noon"            # erst ab 12.00 Uhr (SO: 1. Mai)

# Teilweise gültige Tage: (Hinweis-Schlüssel, standardmässig eingeschaltet?)
_PARTIAL = {
    ("FR", "fronleichnam"): ("fr_lac", True), ("FR", "himmelfahrt"): ("fr_lac", True),
    ("FR", "allerheiligen"): ("fr_lac", True), ("FR", "empfaengnis"): ("fr_lac", True),
    ("SO", "josef"): ("so_communes", False),
    ("AI", "mauritius"): ("ai_inner", True),
    ("NE", "fronleichnam"): ("ne_landeron", False),
}

# Kanton → (Spalte a, Spalte b). Einträge «code» oder «code/besonderheit». Reihenfolge wie in der BJ-Liste.
TABLE: dict[str, tuple[str, str]] = {
    "ZH": ("neujahr berchtold karfreitag ostermontag mai auffahrt pfingstmontag weihnachten stephan", ""),
    "BE": ("neujahr berchtold karfreitag ostermontag auffahrt pfingstmontag weihnachten stephan", ""),
    "LU": ("neujahr berchtold karfreitag ostermontag auffahrt pfingstmontag fronleichnam himmelfahrt allerheiligen empfaengnis"
           " weihnachten stephan", ""),
    "UR": ("neujahr dreikoenige josef karfreitag ostermontag auffahrt pfingstmontag fronleichnam himmelfahrt allerheiligen"
           " empfaengnis weihnachten stephan/xmas", ""),
    "SZ": ("neujahr dreikoenige josef karfreitag ostermontag auffahrt pfingstmontag fronleichnam himmelfahrt allerheiligen"
           " empfaengnis weihnachten stephan", ""),
    "OW": ("neujahr karfreitag auffahrt fronleichnam himmelfahrt bruderklaus allerheiligen empfaengnis weihnachten",
           "berchtold ostermontag pfingstmontag stephan"),
    "NW": ("neujahr josef karfreitag auffahrt fronleichnam himmelfahrt allerheiligen empfaengnis weihnachten",
           "berchtold ostermontag pfingstmontag stephan"),
    "GL": ("neujahr karfreitag ostermontag fahrt_gl auffahrt pfingstmontag allerheiligen weihnachten stephan", "berchtold"),
    "ZG": ("neujahr karfreitag auffahrt fronleichnam himmelfahrt allerheiligen empfaengnis weihnachten",
           "berchtold ostermontag pfingstmontag stephan"),
    "FR": ("neujahr karfreitag auffahrt fronleichnam himmelfahrt allerheiligen empfaengnis weihnachten",
           "berchtold ostermontag pfingstmontag stephan"),
    "SO": ("neujahr josef karfreitag mai/noon auffahrt fronleichnam himmelfahrt allerheiligen weihnachten",
           "berchtold ostermontag pfingstmontag stephan"),
    "BS": ("neujahr karfreitag ostermontag mai auffahrt pfingstmontag weihnachten stephan", ""),
    "BL": ("neujahr karfreitag ostermontag mai auffahrt pfingstmontag weihnachten stephan", ""),
    "SH": ("neujahr karfreitag ostermontag mai auffahrt pfingstmontag weihnachten stephan", "berchtold"),
    "AR": ("neujahr karfreitag ostermontag auffahrt pfingstmontag weihnachten stephan/xmas", ""),
    "AI": ("neujahr karfreitag ostermontag auffahrt pfingstmontag fronleichnam himmelfahrt mauritius allerheiligen empfaengnis"
           " weihnachten stephan/xmas", ""),
    "SG": ("neujahr karfreitag ostermontag auffahrt pfingstmontag allerheiligen weihnachten stephan", "berchtold"),
    "GR": ("neujahr ostermontag auffahrt pfingstmontag weihnachten stephan", "karfreitag"),
    "AG": ("neujahr berchtold karfreitag ostermontag mai auffahrt pfingstmontag fronleichnam himmelfahrt allerheiligen empfaengnis"
           " weihnachten stephan", ""),
    "TG": ("neujahr berchtold karfreitag ostermontag mai auffahrt pfingstmontag weihnachten stephan", ""),
    "TI": ("neujahr dreikoenige josef ostermontag mai auffahrt pfingstmontag fronleichnam peterpaul himmelfahrt allerheiligen"
           " empfaengnis weihnachten stephan", ""),
    "VD": ("neujahr berchtold karfreitag ostermontag auffahrt pfingstmontag bettagsmontag weihnachten", ""),
    "VS": ("neujahr josef auffahrt fronleichnam himmelfahrt allerheiligen empfaengnis weihnachten",
           "berchtold ostermontag pfingstmontag stephan"),
    "NE": ("neujahr berchtold/sunday republique_ne karfreitag auffahrt fronleichnam weihnachten stephan/sunday mai", ""),
    "GE": ("neujahr karfreitag ostermontag auffahrt pfingstmontag jeune_ge weihnachten restauration_ge", ""),
    "JU": ("neujahr berchtold karfreitag ostermontag mai auffahrt pfingstmontag fronleichnam plebiscite_ju himmelfahrt"
           " allerheiligen weihnachten", ""),
}

CODES = tuple(dict.fromkeys(list(_FIXED) + list(_EASTER_OFFSET) + ["fahrt_gl", "jeune_ge", "bettagsmontag"]))


@dataclass(frozen=True)
class Holiday:
    code: str
    day: date
    kind: str                     # "a" gesetzlich | "b" wie Feiertag behandelt | "bund" Bundesfeiertag
    note: str | None = None       # Hinweis-Schlüssel, wenn nur teilweise gültig (agenda.hol.note.<note>)
    default_on: bool = True
    from_time: str | None = None  # "12:00" = erst ab Mittag frei


def easter(year: int) -> date:
    """Ostersonntag im gregorianischen Kalender (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def date_of(code: str, year: int) -> date:
    if code in _FIXED:
        m, d = _FIXED[code]
        return date(year, m, d)
    if code in _EASTER_OFFSET:
        return easter(year) + timedelta(days=_EASTER_OFFSET[code])
    if code == "fahrt_gl":
        day = _nth_weekday(year, 4, 3, 1)                       # erster Donnerstag im April
        e = easter(year)
        if e - timedelta(days=7) <= day < e:                    # Karwoche (Palmsonntag bis Karsamstag)
            day += timedelta(days=7)
        return day
    if code == "jeune_ge":
        return _nth_weekday(year, 9, 6, 1) + timedelta(days=4)  # Donnerstag nach dem ersten Sonntag im September
    if code == "bettagsmontag":
        return _nth_weekday(year, 9, 6, 3) + timedelta(days=1)  # Montag nach dem dritten Sonntag im September
    raise KeyError(code)


@lru_cache(maxsize=256)
def for_canton(canton: str, year: int) -> tuple[Holiday, ...]:
    """Alle Feiertage eines Kantons in einem Jahr (inkl. Bundesfeiertag), nach Datum sortiert."""
    canton = canton if canton in TABLE else DEFAULT_CANTON
    out: list[Holiday] = [Holiday("bundesfeier", date_of("bundesfeier", year), "bund")]
    col_a, col_b = TABLE[canton]
    for kind, column in (("a", col_a), ("b", col_b)):
        for entry in column.split():
            code, _, flag = entry.partition("/")
            day = date_of(code, year)
            if flag == XMAS and date_of("weihnachten", year).weekday() in (0, 4):
                continue
            if flag == AFTER_SUNDAY and (day - timedelta(days=1)).weekday() != 6:
                continue
            note, default_on = _PARTIAL.get((canton, code), (None, True))
            out.append(Holiday(code, day, kind, note, default_on, "12:00" if flag == NOON else None))
    return tuple(sorted(out, key=lambda h: (h.day, h.code)))


def between(canton: str, first: date, last: date) -> list[Holiday]:
    out: list[Holiday] = []
    for year in range(first.year, last.year + 1):
        out += [h for h in for_canton(canton, year) if first <= h.day <= last]
    return out
