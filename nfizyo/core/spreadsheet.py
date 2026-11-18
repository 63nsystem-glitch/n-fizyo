"""Zellen für CSV- und Excel-Dateien entschärfen (Formel-Einschleusung, «CSV injection»).

Text, der mit = + - @ oder Tabulator / Zeilenumbruch beginnt, würde Excel/LibreOffice als Formel ausführen
(openpyxl speichert «=…» sogar direkt als Formel). Solche Texte kommen z. B. als Name der Auftraggeberin aus einer
Bankdatei (camt.054) oder aus dem öffentlichen Terminformular. Sie bekommen vorne ein ' – so bleiben sie Text.
Echte Zahlen (int, float, Decimal) und reine Zahlentexte wie «-12.50» bleiben unverändert (Summen funktionieren weiter).
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Iterable

DANGEROUS_START = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
_PLAIN_NUMBER = re.compile(r"[+-]?\d[\d'’ ]*(?:[.,]\d+)?")


def safe_cell(value: Any) -> Any:
    """Ein Wert für eine Tabellenzelle: gefährlicher Text → mit ' davor, alles andere unverändert."""
    if value is None or isinstance(value, (bool, int, float, Decimal)):
        return value
    if not isinstance(value, str):
        return value
    if value.startswith(DANGEROUS_START) and not _PLAIN_NUMBER.fullmatch(value):
        return "'" + value
    return value


def safe_row(values: Iterable[Any]) -> list[Any]:
    return [safe_cell(v) for v in values]
