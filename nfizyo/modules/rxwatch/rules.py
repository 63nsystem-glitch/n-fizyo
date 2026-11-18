"""Regeln für Verordnungen in der Schweiz – reine Funktionen ohne Datenbank (leicht zu prüfen und anzupassen).

Quellen (Stand Okt. 2026): Physioswiss «Häufige Fragen zur Verordnung» (Okt. 2022), KLV Art. 5:
  - eine Verordnung: höchstens 9 Sitzungen
  - Behandlungsbeginn innerhalb von 5 Wochen nach Ausstellung (KVG)
  - bis 4 Verordnungen (36 Sitzungen) ohne Kostengutsprache; ab der 37. Sitzung beantragt die Ärztin / der Arzt sie (KVG)
  - eine unvollständige Verordnung kann nur die Ärztin / der Arzt ergänzen
Die Software ERINNERT nur. Entscheiden und anfragen tun Menschen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ...core.timeutil import today as _zurich_today

START_DAYS = 35        # 5 Wochen
RENEW_LEFT = 2         # ab 2 freien Sitzungen an die Folgeverordnung denken
KOGU_LIMIT = 36        # ohne Kostengutsprache
KOGU_WARN = 30         # früh warnen
SOON_DAYS = 7          # «bald» = innerhalb einer Woche
NUDGE_DAYS = 7         # Anfrage ohne Antwort → nachfassen

LEVELS = ("ok", "info", "soon", "act")


@dataclass
class Flag:
    code: str
    level: str
    params: dict = field(default_factory=dict)
    request_kind: str | None = None   # welche Anfrage diese Meldung erledigt (renewal, kogu, correction, new)


@dataclass
class RxInput:
    prescription: dict              # id, sessions, issued_on, diagnosis, referrer_id
    law: str | None                 # KVG, UVG, MVG, IVG, private …
    used: int                       # erledigte (oder angemeldete) Sitzungen dieser Verordnung
    future: list[date]              # gebuchte Termine ab heute, die diese Verordnung braucht (aufsteigend)
    chain_used: int = 0             # Sitzungen der letzten 12 Monate über alle Verordnungen (für die 36er-Grenze)
    requests: list[dict] = field(default_factory=list)   # offene Anfragen: {"kind", "date": date, "via"}
    renewal_draft: int | None = None                      # Gesuch-Entwurf im Berichte-Modul (id)
    kogu_ok: bool = False                                 # gültige Kostengutsprache (aus der Abrechnung)
    today: date = field(default_factory=_zurich_today)   # Zürcher Datum, nie das des Servers


def _max(levels: list[str]) -> str:
    return max(levels, key=LEVELS.index) if levels else "ok"


def _request(inp: RxInput, *kinds: str) -> dict | None:
    rows = [r for r in inp.requests if r["kind"] in kinds]
    return max(rows, key=lambda r: r["date"]) if rows else None


def _requested_flag(inp: RxInput, req: dict, urgent: bool) -> Flag:
    days = (inp.today - req["date"]).days
    nudge = days >= NUDGE_DAYS
    return Flag(f"{req['kind']}_requested", "act" if (nudge and urgent) else ("soon" if nudge else "info"),
                {"date": req["date"], "via": req["via"], "days": days, "nudge": nudge})


def evaluate(inp: RxInput) -> dict:
    pr, today = inp.prescription, inp.today
    total = int(pr["sessions"] or 0)
    left = max(total - inp.used, 0)
    flags: list[Flag] = []
    kvg = (inp.law or "KVG").upper() == "KVG"

    # 1. Unvollständig (nur die Ärztin / der Arzt kann ergänzen)
    missing = [k for k, v in (("diagnosis", pr.get("diagnosis")), ("referrer", pr.get("referrer_id")),
                               ("issued_on", pr.get("issued_on"))) if not v]
    if missing:
        req = _request(inp, "correction")
        if req:
            flags.append(_requested_flag(inp, req, bool(inp.future)))
        else:
            flags.append(Flag("incomplete", "act" if inp.future or inp.used == 0 else "soon", {"fields": missing},
                              "correction"))

    # 2. Behandlungsbeginn innerhalb von 5 Wochen (KVG)
    start_deadline = None
    if kvg and pr.get("issued_on") and inp.used == 0:
        start_deadline = date.fromisoformat(pr["issued_on"]) + timedelta(days=START_DAYS)
        req = _request(inp, "new")
        if req:
            flags.append(_requested_flag(inp, req, True))
        elif today > start_deadline:
            flags.append(Flag("start_missed", "act", {"deadline": start_deadline}, "new"))
        elif inp.future and inp.future[0] > start_deadline:
            flags.append(Flag("start_late", "act", {"date": inp.future[0], "deadline": start_deadline}, "new"))
        elif not inp.future and (start_deadline - today).days <= SOON_DAYS:
            flags.append(Flag("start_soon", "soon", {"deadline": start_deadline, "days": (start_deadline - today).days}))

    # 3. Folgeverordnung: gebuchte Termine über die freien Sitzungen hinaus, oder nur noch wenige frei
    first_uncovered = inp.future[left] if len(inp.future) > left else None
    covered_until = inp.future[left - 1] if left and len(inp.future) >= left else None
    req = _request(inp, "renewal")
    if req:
        urgent = first_uncovered is not None and (first_uncovered - today).days <= SOON_DAYS
        flags.append(_requested_flag(inp, req, urgent))
    elif first_uncovered is not None:
        level = "act" if (first_uncovered - today).days <= SOON_DAYS else "soon"
        flags.append(Flag("uncovered", level, {"date": first_uncovered, "booked": len(inp.future), "left": left}, "renewal"))
    elif total and left <= RENEW_LEFT and (inp.future or left > 0):
        flags.append(Flag("renew", "soon", {"left": left, "total": total}, "renewal"))
    if inp.renewal_draft and not req:
        flags.append(Flag("renewal_draft", "info", {"id": inp.renewal_draft}))

    # 4. Kostengutsprache ab der 37. Sitzung (KVG) – entfällt, wenn eine gültige Kostengutsprache vorliegt
    if kvg and not inp.kogu_ok:
        planned = inp.chain_used + len(inp.future)
        req = _request(inp, "kogu")
        if req and (planned > KOGU_LIMIT or inp.chain_used >= KOGU_WARN):
            flags.append(_requested_flag(inp, req, False))
        elif planned > KOGU_LIMIT:
            idx = KOGU_LIMIT - inp.chain_used
            when = inp.future[idx] if 0 <= idx < len(inp.future) else None
            level = "act" if when is None or (when - today).days <= 2 * SOON_DAYS else "soon"
            flags.append(Flag("kogu", level, {"date": when, "n": inp.chain_used, "limit": KOGU_LIMIT}, "kogu"))
        elif inp.chain_used >= KOGU_WARN:
            flags.append(Flag("kogu_soon", "soon", {"n": inp.chain_used, "limit": KOGU_LIMIT}, "kogu"))

    return {"flags": flags, "level": _max([f.level for f in flags]), "total": total, "used": inp.used, "left": left,
            "booked": len(inp.future), "first_uncovered": first_uncovered, "covered_until": covered_until,
            "start_deadline": start_deadline, "chain_used": inp.chain_used, "kvg": kvg}
