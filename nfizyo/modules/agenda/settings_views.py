"""Einstellungen der Agenda im Leitungs-Bildschirm: Standorte/Räume/Geräte, Behandlungsarten, Arbeitszeiten."""
from __future__ import annotations

import re

from aiohttp import web

from ...core import patients as core_patients
from ...core.audit import audit
from ...core.keys import DB
from ...core.modules import ModuleContext
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.timeutil import WEEKDAYS
from ...core.web import redirect_with_flash
from ...knowledge import tariffs
from . import repo

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _s(form, key: str, maxlen: int = 300) -> str:
    return str(form.get(key) or "").strip()[:maxlen]


def _id(form, key: str = "id") -> int | None:
    v = str(form.get(key) or "")
    return int(v) if v.isdigit() and len(v) <= 9 else None


async def locations_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/standorte"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        ok = True
        if do == "location" and _s(form, "name", 120):
            repo.add_location(db, _s(form, "name", 120), _s(form, "address") or None, _s(form, "phone", 40) or None)
        elif do in ("room", "device") and _id(form, "location_id") and _s(form, "name", 120):
            ok = repo.add_room_or_device(db, do, _id(form, "location_id"), _s(form, "name", 120))
        elif do == "toggle" and _id(form):
            ok = repo.toggle_active(db, str(form.get("table")), _id(form))
        elif do == "edit_location" and _id(form) and _s(form, "name", 120):
            ok = repo.update_location(db, _id(form), _s(form, "name", 120), _s(form, "address") or None, _s(form, "phone", 40) or None)
        elif do == "rename" and _id(form) and _s(form, "name", 120):
            ok = repo.rename(db, str(form.get("table")), _id(form), _s(form, "name", 120))
        else:
            ok = False
        target = back + (f"?bearbeiten={_id(form, 'location_id')}" if ok and do == "rename" and _id(form, "location_id") else "")
        raise redirect_with_flash(target, "common.saved" if ok else "settings.invalid")
    edit_id = _id(request.query, "bearbeiten")
    return await settings_page(request, "standorte", "agenda/s_locations.html", {"locations": repo.all_locations(db), "edit_id": edit_id})


async def types_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/behandlungen"
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "toggle" and _id(form):
            repo.toggle_active(db, "treatment_types", _id(form))
            raise redirect_with_flash(back, "common.saved")
        nde, nfr = _s(form, "name_de", 120), _s(form, "name_fr", 120)
        minutes = _id(form, "minutes") or 0
        code = _s(form, "tariff_code", 10) or None
        color = _s(form, "color", 7) or "#2F5DA8"
        if not (nde or nfr) or not 5 <= minutes <= 240 or (code and code not in {p.code for p in tariffs.positions("kvg312") if p.group == "session"}) or not COLOR.match(color):
            raise redirect_with_flash(back, "settings.invalid")
        repo.save_treatment_type(db, {"name_de": nde or nfr, "name_fr": nfr or nde, "minutes": minutes, "tariff_code": code,
                                      "needs_device": 1 if form.get("needs_device") else 0, "color": color},
                                 _id(form) if form.get("do") == "update" else None)
        audit(db, "update", "treatment_type", _id(form))
        raise redirect_with_flash(back, "common.saved")
    return await settings_page(request, "behandlungen", "agenda/s_types.html", {
        "types": repo.treatment_types(db, include_inactive=True), "kvg_positions": [p for p in tariffs.positions("kvg312") if p.group == "session"]})


async def hours_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/zeiten"
    therapists = core_patients.therapists(db)
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "remove" and _id(form):
            repo.remove_hours(db, _id(form))
            raise redirect_with_flash(back, "common.saved")
        uid = _id(form, "user_id")
        start, end = _s(form, "start", 5), _s(form, "end", 5)
        days = [int(d) for d in form.getall("weekday", []) if str(d) in "0123456" and len(str(d)) == 1]
        if uid not in [t["id"] for t in therapists] or not days or not HHMM.match(start) or not HHMM.match(end) or start >= end:
            raise redirect_with_flash(back, "settings.invalid")
        repo.add_hours(db, uid, _id(form, "location_id"), days, start, end)
        raise redirect_with_flash(back, "common.saved")
    return await settings_page(request, "zeiten", "agenda/s_hours.html", {
        "therapists": therapists, "hours": repo.all_hours(db), "locations": repo.locations(db),
        "wd": WEEKDAYS.get(request["lang"], WEEKDAYS["de"])})


def setup(ctx: ModuleContext) -> None:
    for key, label, path, order, handler in (
        ("standorte", "settings.sec.standorte", "standorte", 30, locations_page),
        ("behandlungen", "settings.sec.behandlungen", "behandlungen", 40, types_page),
        ("zeiten", "settings.sec.zeiten", "zeiten", 50, hours_page),
    ):
        ctx.page("leitung", f"/einstellungen/{path}", handler, ("GET", "POST"))
        ctx.slot("leitung.settings", order, (lambda k, lb, p: lambda request: {"key": k, "label_key": lb, "path": p})(key, label, path))
