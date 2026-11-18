"""Dosierung als Text: Serien, Wiederholungen, Halten, Dauer, pro Tag – mit richtiger Mehrzahl in allen App-Sprachen.

Patienten-App: ausgeschrieben («3 Serien à 10 Wiederholungen · 5 s halten»). Therapie (DE/FR): kurz («3 × 10 · 5 s halten»).
Fehlende Angaben werden weggelassen – nie ein nacktes «2 ×»."""
from __future__ import annotations

from typing import Any

from ...core.i18n import plural_category


def plural(i18n, lang: str, key: str, n: int) -> str:
    """Text mit Zahl aus key.one / key.few / … / key.other (fehlende Form → other)."""
    cat = plural_category(lang, n)
    full = f"{key}.{cat}"
    if not i18n.has(lang, full):
        full = f"{key}.other"
    return i18n.t(lang, full, n=n)


def _num(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def values(row) -> dict:
    """Dosierung aus einer Übung oder einem Programmpunkt (sqlite-Zeile oder dict)."""
    keys = row.keys() if hasattr(row, "keys") else ()

    def get(*names):
        for name in names:
            if name in keys:
                return row[name]
        return None

    return {"sets": _num(get("sets")), "reps": _num(get("reps")), "hold": _num(get("hold_sec", "hold")),
            "minutes": _num(get("duration_min", "minutes")), "per_day": _num(get("per_day")) or 1}


def text(i18n, lang: str, row, short: bool = False) -> str:
    """Dosierung als ein Satzteil; leer, wenn nichts angegeben ist."""
    d = values(row)
    parts: list[str] = []
    if short:
        if d["sets"] and d["reps"]:
            parts.append(f"{d['sets']}\u00a0×\u00a0{d['reps']}")
        elif d["reps"]:
            parts.append(i18n.t(lang, "hp.dose.reps", n=d["reps"]))
        elif d["sets"]:
            parts.append(plural(i18n, lang, "hp.dose.sets", d["sets"]))
        if d["hold"]:
            parts.append(i18n.t(lang, "hp.dose.hold", n=d["hold"]))
        if d["minutes"]:
            parts.append(i18n.t(lang, "hp.dose.minutes", n=d["minutes"]))
        if d["per_day"] > 1:
            parts.append(i18n.t(lang, "hp.dose.per_day", n=d["per_day"]))
        return " · ".join(parts)
    base = "p.ex.dose"
    if d["sets"] and d["reps"]:
        parts.append(i18n.t(lang, f"{base}.sets_reps", sets=plural(i18n, lang, f"{base}.sets", d["sets"]),
                            reps=plural(i18n, lang, f"{base}.reps", d["reps"])))
    elif d["reps"]:
        parts.append(plural(i18n, lang, f"{base}.reps", d["reps"]))
    elif d["sets"]:
        parts.append(plural(i18n, lang, f"{base}.sets", d["sets"]))
    if d["hold"]:
        parts.append(plural(i18n, lang, f"{base}.hold", d["hold"]))
    if d["minutes"]:
        parts.append(plural(i18n, lang, f"{base}.minutes", d["minutes"]))
    if d["per_day"] > 1:
        parts.append(plural(i18n, lang, f"{base}.per_day", d["per_day"]))
    return " · ".join(parts)
