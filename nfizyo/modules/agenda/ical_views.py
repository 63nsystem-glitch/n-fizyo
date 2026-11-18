"""Kalender-Abo: öffentlicher Feed (geheimer Link) und Verwaltung durch die Therapeutin / den Therapeuten."""
from __future__ import annotations

from aiohttp import web

from ...core.keys import DB
from ...core.screens import prefix
from ...core.tenant import practice_scope
from ...core.web import public_url, redirect_with_flash, render
from . import ical


async def feed(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    row = ical.lookup(db, request.match_info["token"])
    if row is None:
        raise web.HTTPNotFound()
    with practice_scope(row["practice_id"], row["user_id"]):
        practice = db.one("SELECT name FROM practices WHERE id = ?", (row["practice_id"],))
        user = db.one("SELECT name, lang FROM users WHERE id = ?", (row["user_id"],))
        body = ical.build(db, practice_id=row["practice_id"], user_id=row["user_id"], detail=row["detail"],
                          calendar_name=f"{practice['name']} – {user['name']}", lang=user["lang"] or "de")
        ical.touch(db, row)
    return web.Response(text=body, content_type="text/calendar", charset="utf-8",
                        headers={"Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex"})


async def manage(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/agenda/kalender-abo"
    new_url = None
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "create":
            token = ical.create(db, str(form.get("label") or "").strip(), str(form.get("detail") or "initials"))
            new_url = public_url(request, f"/kalender/{token}.ics")
        elif form.get("do") == "revoke" and str(form.get("token_id", "")).isdigit() and len(str(form.get("token_id"))) <= 9:
            ical.revoke(db, int(form["token_id"]))
            raise redirect_with_flash(back, "agenda.ical.revoked")
        else:
            raise redirect_with_flash(back, "agenda.ical.invalid")
    resp = await render(request, "agenda/ical.html", {"tokens": ical.mine(db), "new_url": new_url,
                                                      "webcal": new_url.replace("https://", "webcal://").replace("http://", "webcal://") if new_url else None})
    resp.headers["Cache-Control"] = "no-store"
    return resp
