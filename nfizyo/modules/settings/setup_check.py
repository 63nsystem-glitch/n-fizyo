"""Einrichtungs-Checkliste für eine neue Praxis (Leitung › Übersicht): was fehlt noch, bevor der Alltag läuft?

Jeder Punkt prüft echte Daten (nicht ein Häkchen, das man setzt) und führt direkt zur passenden Einstellung.
Andere Module werden nur über ihre Schnittstelle gelesen (Agenda: Standorte, Behandlungsarten, Arbeitszeiten;
Abrechnung: IBAN, KVG-Taxpunktwert). Ist ein Modul aus, entfällt sein Punkt.
Freiwillige Punkte (optional=True, z. B. SMS-Anbieter – wird von NSystem im Server eingerichtet) zählen nicht zum Fortschritt
und halten die Checkliste nie offen. Räume sind freiwillig (ein Standort genügt). Die Leitung kann die Checkliste jederzeit
ausblenden (und unter /leitung/einrichtung wieder zeigen).
"""
from __future__ import annotations

import re

from aiohttp import web

from ...core import patients as core_patients
from ...core.audit import audit
from ...core.db import utcnow
from ...core.keys import DB, MAIL, SMS
from ...core.modules import enabled
from ...core.screens import prefix, roles_of
from ...core.tenant import current_practice_id, current_user_id
from ...core.web import redirect_with_flash, render


def items(request: web.Request) -> list[dict]:
    db = request.app[DB]
    pid = current_practice_id()
    S = prefix(request)
    pr = db.one("SELECT * FROM practices WHERE id = ?", (pid,))
    users = db.query("SELECT * FROM users WHERE practice_id = ? AND active = 1", (pid,))
    therapists = [u for u in users if "therapist" in roles_of(u)]
    out = [{"key": "practice", "href": f"{S}/einstellungen/praxis",
            "done": bool(pr["name"] and pr["street"] and pr["zip"] and pr["city"] and (pr["phone"] or pr["email"])
                         and pr["gln"] and re.fullmatch(r"\d{13}", pr["gln"]) and pr["zsr"])}]
    if enabled(request, "billing"):
        from ..billing import api as billing_api

        out.append({"key": "payment", "href": f"{S}/einstellungen/zahlung", "done": billing_api.payment_iban(db) is not None})
        out.append({"key": "tariff", "href": f"{S}/einstellungen/tarife", "done": billing_api.kvg_tp_value(db) is not None})
    from ..agenda import api as agenda_api

    lookup = agenda_api.lookup_tables(db)
    out.append({"key": "locations", "href": f"{S}/einstellungen/standorte", "done": bool(agenda_api.locations(db)),
                "no_rooms": not agenda_api.rooms(db)})
    out.append({"key": "types", "href": f"{S}/einstellungen/behandlungen", "done": bool(lookup["types"])})
    out.append({"key": "hours", "href": f"{S}/einstellungen/zeiten",
                "done": bool(therapists) and all(agenda_api.available_minutes_per_week(db, u["id"]) > 0 for u in therapists)})
    out.append({"key": "insurers", "href": f"{S}/einstellungen/versicherer", "done": bool(core_patients.insurers(db))})
    out.append({"key": "team", "href": f"{S}/team",
                "done": bool(therapists) and all(u["gln"] and re.fullmatch(r"\d{13}", u["gln"]) for u in therapists)})
    out.append({"key": "messaging", "href": f"{S}/einstellungen/sms", "done": request.app[SMS].provider_of(pid) != "log",
                "mail": request.app[MAIL].live, "optional": True})
    out.append({"key": "mfa", "href": f"{S}/team", "done": bool(users) and all(u["totp_enabled_at"] for u in users)})
    return out


def progress(rows: list[dict]) -> dict:
    """Fortschritt nur über die Pflichtpunkte – freiwillige Punkte halten die Checkliste nie offen."""
    required = [r for r in rows if not r.get("optional")]
    done = sum(1 for r in required if r["done"])
    return {"items": rows, "done": done, "total": len(required), "complete": done == len(required)}


def dismissed(db, practice_id: int) -> bool:
    return bool(db.value("SELECT dismissed_at FROM practice_setup WHERE practice_id = ?", (practice_id,)))


async def card(request: web.Request) -> dict | None:
    """Karte auf der Übersicht der Leitung – bis alles erledigt ist und die Leitung sie ausblendet."""
    if dismissed(request.app[DB], current_practice_id()):
        return None
    return {**progress(items(request)), "page": False}


async def page(request: web.Request) -> web.StreamResponse:
    """Ganze Checkliste (auch nach dem Ausblenden erreichbar); POST blendet die Karte und die Kachel aus – jederzeit, auch mit
    offenen Punkten (die Seite bleibt erreichbar und zeigt sie weiter)."""
    db = request.app[DB]
    pid = current_practice_id()
    back = f"{prefix(request)}/uebersicht"
    state = progress(items(request))
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "show":
            db.execute("DELETE FROM practice_setup WHERE practice_id = ?", (pid,))
            raise redirect_with_flash(back, "common.saved")
        if form.get("do") != "dismiss":
            raise redirect_with_flash(f"{prefix(request)}/einrichtung", "settings.invalid")
        db.execute("INSERT INTO practice_setup (practice_id, dismissed_at, dismissed_by) VALUES (?,?,?)"
                   " ON CONFLICT(practice_id) DO UPDATE SET dismissed_at = excluded.dismissed_at, dismissed_by = excluded.dismissed_by",
                   (pid, utcnow(), current_user_id()))
        audit(db, "update", "practice_setup", pid, None, {"dismissed": True, "open": state["total"] - state["done"]})
        raise redirect_with_flash(back, "setup.dismissed")
    return await render(request, "settings/setup.html", {"ctx": {**state, "page": True}, "hidden": dismissed(db, pid)})


async def tile(request: web.Request) -> dict | None:
    """Kachel oben auf der Übersicht, solange die Einrichtung nicht vollständig ist (führt zur Checkliste)."""
    if dismissed(request.app[DB], current_practice_id()):
        return None
    state = progress(items(request))
    if state["complete"]:
        return None
    return {"label_key": "setup.tile", "value": f"{state['done']}/{state['total']}", "sub_key": "setup.tile_sub",
            "href": f"{prefix(request)}/einrichtung"}
