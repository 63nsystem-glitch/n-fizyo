"""Abwesenheiten und Schliessungen (Empfang und Leitung verwalten alle; Therapie sieht die eigenen) und Feiertage (Leitung).

/empfang/agenda/abwesenheiten, /leitung/agenda/abwesenheiten   Liste, neu, bearbeiten, löschen; betroffene Termine
/therapie/agenda/abwesenheiten                                  eigene Abwesenheiten, Schliessungen, Feiertage (nur lesen)
/leitung/einstellungen/feiertage                                Kanton und einzelne Feiertage ein/aus
"""
from __future__ import annotations

from datetime import date, timedelta

from aiohttp import web

from ...core import patients as core_patients
from ...core.keys import DB
from ...core.modules import ModuleContext
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.timeutil import day_bounds_utc, from_iso, today
from ...core.web import int_param, redirect_with_flash, render
from . import absences, holidays, repo

AHEAD_DAYS = 120


def _therapist_ids(db) -> set[int]:
    return {t["id"] for t in core_patients.therapists(db)}


def affected(db, row) -> list[dict]:
    """Geplante Termine, die in eine Abwesenheit / Schliessung fallen (damit der Empfang sie verschieben kann)."""
    first, last = date.fromisoformat(row["starts_on"]), date.fromisoformat(row["ends_on"])
    lo, _ = day_bounds_utc(first)
    _, hi = day_bounds_utc(last)
    offs = [o for o in absences.periods(db, first, last, user_id=row["user_id"]) if o.absence_id == row["id"]]
    out = []
    for a in repo.between(db, lo, hi, therapist_id=row["user_id"], include_cancelled=False):
        if a["status"] not in ("booked", "arrived") or not a["patient_id"]:
            continue
        s, e = from_iso(a["starts_at"]), from_iso(a["ends_at"])
        if any(o.overlaps(s, e) for o in offs):
            out.append({"a": a, "start": s, "end": e})
    return out


def _form_values(row) -> dict:
    if row is None:
        return {"who": "", "kind": "vacation", "starts_on": today().isoformat(), "ends_on": today().isoformat(), "part": "day",
                "start_time": "", "end_time": "", "note": ""}
    return {"who": "praxis" if row["user_id"] is None else str(row["user_id"]), "kind": row["kind"], "starts_on": row["starts_on"],
            "ends_on": row["ends_on"], "part": "time" if row["start_time"] else "day", "start_time": row["start_time"] or "",
            "end_time": row["end_time"] or "", "note": row["note"] or ""}


def _clean(form, db) -> tuple[dict, list[str], dict]:
    who = str(form.get("who") or "")
    values = {k: form.get(k) for k in ("kind", "starts_on", "ends_on", "part", "start_time", "end_time", "note")}
    values["who"] = who
    if who == "praxis":
        values["kind"] = "closure"
    else:
        values["user_id"] = who
        if values.get("kind") == "closure":
            values["kind"] = ""
    clean, errors = absences.validate(values, _therapist_ids(db))
    if who != "praxis" and "user_id" in errors:
        errors = [e for e in errors if e != "user_id"] + ["who"]
    return clean, errors, values


async def _list_ctx(request: web.Request, *, show_all: bool) -> dict:
    db = request.app[DB]
    d0 = today()
    since = None if show_all else d0 - timedelta(days=7)
    rows = absences.listing(db, since=since)
    hol = absences.active_holidays(db, d0, d0 + timedelta(days=AHEAD_DAYS))
    return {"rows": rows, "holidays": hol, "canton": absences.canton(db), "therapists": core_patients.therapists(db),
            "kinds": absences.KINDS, "show_all": show_all, "today": d0}


async def manage(request: web.Request) -> web.StreamResponse:
    """Empfang / Leitung: alle Abwesenheiten und Schliessungen der Praxis."""
    db = request.app[DB]
    back = f"{prefix(request)}/agenda/abwesenheiten"
    errors: list[str] = []
    values = _form_values(None)
    if request.method == "POST":
        form = await request.post()
        clean, errors, values = _clean(form, db)
        if not errors:
            aid = absences.save(db, clean)
            raise redirect_with_flash(f"{back}/{aid}?neu=1", "agenda.abs.saved")
    ctx = await _list_ctx(request, show_all=request.query.get("alle") == "1")
    return await render(request, "agenda/absences.html", {**ctx, "v": values, "errors": errors, "editable": True},
                        status=400 if errors else 200)


async def edit(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    row = absences.get(db, int_param(request, "abs_id"))
    if row is None:
        raise web.HTTPNotFound()
    back = f"{prefix(request)}/agenda/abwesenheiten"
    errors: list[str] = []
    values = _form_values(row)
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "delete":
            absences.delete(db, row["id"])
            raise redirect_with_flash(back, "agenda.abs.deleted")
        clean, errors, values = _clean(form, db)
        if not errors:
            absences.save(db, clean, row["id"])
            raise redirect_with_flash(f"{back}/{row['id']}?neu=1", "agenda.abs.saved")
    return await render(request, "agenda/absence.html", {
        "row": row, "v": values, "errors": errors, "affected": affected(db, row), "therapists": core_patients.therapists(db),
        "kinds": absences.KINDS, "just_saved": request.query.get("neu") == "1"}, status=400 if errors else 200)


async def own(request: web.Request) -> web.StreamResponse:
    """Therapie: eigene Abwesenheiten, Schliessungen der Praxis und Feiertage – nur lesen."""
    db = request.app[DB]
    d0 = today()
    rows = absences.listing(db, user_id=request["user"]["id"], since=d0 - timedelta(days=7))
    return await render(request, "agenda/absences.html", {
        "rows": rows, "holidays": absences.active_holidays(db, d0, d0 + timedelta(days=AHEAD_DAYS)), "canton": absences.canton(db),
        "editable": False, "show_all": False, "today": d0, "kinds": absences.KINDS, "therapists": [], "v": {}, "errors": []})


# --------------------------------------------------------------------------- Leitung › Einstellungen › Feiertage
async def holidays_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/feiertage"
    this_year = today().year
    try:
        year = int(request.query.get("jahr") or this_year)
    except ValueError:
        year = this_year
    year = min(max(year, this_year - 1), this_year + 3)
    if request.method == "POST":
        form = await request.post()
        ok = False
        if form.get("do") == "canton":
            ok = absences.save_canton(db, str(form.get("canton") or ""))
        elif form.get("do") == "toggle":
            ok = absences.set_enabled(db, absences.canton(db), str(form.get("code") or ""), form.get("enabled") == "1")
        jahr = str(form.get("jahr") or "")
        raise redirect_with_flash(back + (f"?jahr={jahr}" if jahr.isdigit() else ""), "common.saved" if ok else "settings.invalid")
    return await settings_page(request, "feiertage", "agenda/s_holidays.html", {
        "setting": absences.holiday_setting(db), "cantons": holidays.CANTONS, "items": absences.year_list(db, year), "year": year,
        "years": list(range(this_year - 1, this_year + 4)), "closures": absences.listing(db, since=today(), only_closures=True)})


def setup(ctx: ModuleContext) -> None:
    for screen in ("empfang", "leitung"):
        ctx.page(screen, "/agenda/abwesenheiten", manage, ("GET", "POST"))
        ctx.page(screen, "/agenda/abwesenheiten/{abs_id:\\d+}", edit, ("GET", "POST"))
    ctx.page("therapie", "/agenda/abwesenheiten", own)
    ctx.page("leitung", "/einstellungen/feiertage", holidays_page, ("GET", "POST"))
    ctx.slot("leitung.settings", 55, lambda request: {"key": "feiertage", "label_key": "agenda.hol.settings_title", "path": "feiertage"})
