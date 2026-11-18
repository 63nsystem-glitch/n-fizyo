"""Hilfe › Support anfragen (Therapie, Empfang, Leitung).

- Bereich, Dringlichkeit, kurze Beschreibung; die Leitung kann den Supportzugang gleich mit öffnen (Einverständnis).
- Patientendaten dürfen nicht hinaus: klare Warnung, und Senden wird blockiert, wenn der Text offensichtliche Patientendaten
  enthält (Namen von Patient/innen dieser Praxis, Geburtsdaten, AHV-, Versicherten-, Telefonnummern, E-Mail-Adressen –
  core/patientdata.py). Blockierter Text wird nicht gespeichert und nicht protokolliert.
- Übermittlung mit der nächsten Meldung der Box (signiert, agent/client.py); die Antwort von NSystem kommt genauso zurück.
  Ist der Server nicht mit NSystem verbunden (core/box.configured), sagt die Seite das ehrlich: Anfragen werden gespeichert,
  aber als «nicht gesendet» gezeigt, mit dem Hinweis auf info@nsystem.ch.
"""
from __future__ import annotations

from aiohttp import web

from ...core import box, nsystem, patientdata
from ...core.audit import audit
from ...core.keys import DB, SETTINGS
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix, roles_of
from ...core.tenant import current_practice_id
from ...core.web import redirect_with_flash, render


def _is_owner(request) -> bool:
    """Leitung (Rolle owner): sieht alle Anfragen der Praxis und kann den Supportzugang gleich mit öffnen."""
    return "owner" in roles_of(request["user"])


async def help_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    settings = request.app[SETTINGS]
    pid = current_practice_id()
    user = request["user"]
    owner = _is_owner(request)
    connected = box.configured(settings)
    back = f"{prefix(request)}/hilfe"
    form_values = {"area": "technical", "urgency": "normal", "body": "", "hours": nsystem.SUPPORT_DEFAULT_HOURS}
    blocked: list[str] = []
    error = None
    if request.method == "POST":
        form = await request.post()
        area, urgency = str(form.get("area") or ""), str(form.get("urgency") or "")
        body = str(form.get("body") or "").strip()[:nsystem.MAX_BODY]
        with_access = owner and bool(form.get("with_access"))
        hours = int(form.get("hours")) if str(form.get("hours") or "").isdigit() else nsystem.SUPPORT_DEFAULT_HOURS
        form_values = {"area": area, "urgency": urgency, "body": body, "hours": hours, "with_access": with_access}
        if area not in nsystem.AREAS or urgency not in nsystem.URGENCIES or not body:
            error = "support.invalid"
        elif with_access and (hours not in nsystem.SUPPORT_HOURS or not nsystem.support_configured(settings)
                              or not nsystem.support_allowed(db, settings)):
            error = "support.access_unavailable"
        else:
            blocked = patientdata.scan(body, db, pid)
            if not blocked:
                uid = nsystem.create_request(db, practice_id=pid, user_id=user["id"], area=area, urgency=urgency, body=body,
                                             with_access=with_access)
                audit(db, "support_request", "support", None, None, {"area": area, "urgency": urgency, "uid": uid})
                if with_access and not nsystem.support_status(settings)["open"] and nsystem.pending_session(db) is None:
                    nsystem.open_support(db, settings, practice_id=pid, user_id=user["id"], hours=hours)
                    audit(db, "support_access_open", "support", None, None, {"hours": hours})
                if not connected:
                    raise redirect_with_flash(back, "support.sent_offline")
                raise redirect_with_flash(back, "support.sent_access" if with_access else "support.sent")
    rows = nsystem.requests_for(db, pid)
    if not owner:
        rows = [r for r in rows if r["user_id"] == user["id"]]
    unread = {r["uid"] for r in rows if r["reply_at"] and not r["reply_seen_at"]}
    if unread and request.method == "GET":
        nsystem.mark_replies_seen(db, pid, None if owner else user["id"])
    return await render(request, "support/help.html", {
        "areas": nsystem.AREAS, "urgencies": nsystem.URGENCIES, "hours_options": nsystem.SUPPORT_HOURS, "values": form_values,
        "blocked": blocked, "error": error, "requests": rows, "unread": unread, "owner": owner, "connected": connected,
        "access_possible": owner and nsystem.support_configured(settings) and nsystem.support_allowed(db, settings),
        "access": nsystem.support_status(settings)}, status=422 if (blocked or error) else 200)


def badge_replies(db, practice_id: int, user) -> int:
    return nsystem.unread_replies(db, practice_id, None if "owner" in roles_of(user) else user["id"])


def setup(ctx: ModuleContext) -> None:
    ctx.badge("support_replies", badge_replies)
    for screen in ("therapie", "empfang", "leitung"):
        ctx.page(screen, "/hilfe", help_page, ("GET", "POST"))
        ctx.nav(screen, NavItem("nav.help", "help", "/hilfe", order=99, badge="support_replies"))
