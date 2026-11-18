"""Gemeinsame Schweizer Formate für Oberfläche, PDF und Texte – überall dieselben Helfer benutzen.

- Geld:     money(1290) → «1’290.00», chf(1290) → «CHF 1’290.00» (negativ mit «−»)
- Zahlen:   decimal(2.5, "fr") → «2,5» (Gewicht, Messwerte); percent(8.1, "de") → «8.1 %», percent(8.1, "fr") → «8,1 %»
            (eine Schreibweise pro Sprache; Eingabefelder bleiben mit Punkt)
- Telefon:  phone("+41790000099") → «079 000 00 99» (Schweiz); andere Länder «+49 30 1234567» unverändert gruppiert
- Namen:    person_name(p) → «Peter Muster» (Überschriften, Fliesstext); person_name(p, sorted_=True) → «Muster, Peter»
            (Listen, die nach Nachnamen sortiert sind, Auswahllisten)
- Zeitraum: date_range(a, b, lang) → «30.10.2026» (ein Tag) oder «30.10.2026 – 02.11.2026»
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

THIN_GROUP = "’"          # Tausender-Trennzeichen wie auf Rechnungen und QR-Zahlteil (1’290.00)
NBSP = " "


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value: Any, digits: int = 2) -> str:
    """1290 → «1’290.00»; leer/ungültig → «–»."""
    f = _float(value)
    if f is None:
        return "–"
    f = round(f, digits)
    return ("−" if f < 0 else "") + f"{abs(f):,.{digits}f}".replace(",", THIN_GROUP)


def chf(value: Any, digits: int = 2) -> str:
    """1290 → «CHF 1’290.00» (negativ «−CHF 12.50»)."""
    text = money(value, digits)
    if text == "–":
        return text
    return ("−CHF " + text[1:]) if text.startswith("−") else "CHF " + text


DECIMAL_COMMA = {"fr", "it", "pt", "es", "sq", "bks", "tr", "uk"}


def decimal(value: Any, lang: str = "de", digits: int = 1) -> str:
    """Zahl ohne unnötige Nachkommastellen, Dezimalzeichen der Sprache: DE «2.5», FR «2,5» (Geld immer mit Punkt)."""
    f = _float(value)
    if f is None:
        return "" if value is None else str(value)
    text = str(int(round(f))) if abs(f - round(f)) < 1e-9 else f"{f:.{digits}f}"
    return text.replace(".", ",") if lang in DECIMAL_COMMA else text


def percent(value: Any, lang: str = "de", digits: int = 1) -> str:
    """Prozent mit einer Schreibweise pro Sprache: DE «8.1 %», FR «8,1 %» (geschütztes Leerzeichen vor %)."""
    f = _float(value)
    if f is None:
        return "–"
    text = f"{f:.{digits}f}"
    if digits and text.endswith("0" * digits):          # 25.0 → 25
        text = text.split(".")[0]
    if lang in DECIMAL_COMMA:
        text = text.replace(".", ",")
    return text + NBSP + "%"


def phone(value: Any) -> str:
    """Telefonnummer für die Anzeige: Schweiz «079 000 00 04», sonst «+49 30 1234567» (unbekannt: wie gespeichert)."""
    if not value:
        return ""
    raw = str(value).strip()
    s = re.sub(r"[\s\-/().]", "", raw)
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+41") and len(s) == 12 and s[3:].isdigit():
        n = "0" + s[3:]                                   # 0790000004
    elif s.startswith("0") and len(s) == 10 and s.isdigit():
        n = s
    else:
        if re.fullmatch(r"\+\d{8,15}", s):               # Ausland: Landesvorwahl abtrennen, Rest in Dreiergruppen
            cc_len = 2 if s[1:3] in _TWO_DIGIT_CC else 3
            rest = s[1 + cc_len:]
            return f"+{s[1:1 + cc_len]} " + " ".join(rest[i:i + 3] for i in range(0, len(rest), 3))
        return raw
    if n.startswith(("0800", "0840", "0842", "0844", "0848", "0900", "0901", "0906")):
        return f"{n[:4]} {n[4:7]} {n[7:]}"                # 0800 123 456
    return f"{n[:3]} {n[3:6]} {n[6:8]} {n[8:]}"           # 079 000 00 04


# Zweistellige Landesvorwahlen (die häufigsten in der Schweiz)
_TWO_DIGIT_CC = {"30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47", "48", "49", "90", "91",
                 "92", "93", "94", "95", "98", "20", "27", "51", "52", "54", "55", "56", "57", "58", "60", "61", "62", "63",
                 "64", "65", "66", "81", "82", "84", "86"}


def _get(p: Any, key: str) -> str:
    if p is None:
        return ""
    try:
        v = p[key]
    except (KeyError, IndexError, TypeError):
        v = getattr(p, key, "")
    return str(v or "").strip()


def person_name(p: Any, sorted_: bool = False, first_key: str = "first_name", last_key: str = "last_name") -> str:
    """«Peter Muster» – oder für Listen nach Nachnamen «Muster, Peter»."""
    first, last = _get(p, first_key), _get(p, last_key)
    if sorted_:
        return f"{last}, {first}" if first and last else (last or first)
    return f"{first} {last}".strip()


def as_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def date_range(start: Any, end: Any, lang: str = "de", weekday: bool = False) -> str:
    """Zeitraum: ein Tag → ein Datum; sonst «von – bis» (gleiches Datumsformat auf beiden Seiten)."""
    from .timeutil import fmt_date

    a, b = as_date(start), as_date(end)
    if a is None and b is None:
        return ""
    if b is None or a == b:
        return fmt_date(a or b, lang, weekday=weekday)
    if a is None:
        return fmt_date(b, lang, weekday=weekday)
    return f"{fmt_date(a, lang, weekday=weekday)} – {fmt_date(b, lang, weekday=weekday)}"
