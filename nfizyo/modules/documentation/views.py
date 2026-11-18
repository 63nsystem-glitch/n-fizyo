"""Verlauf (Notizen) in der Akte des Therapie-Bildschirms: Notiz schreiben, freigeben, Nachtrag.

Nur im Therapie-Bildschirm – Empfang und Leitung sehen keine Notizen."""
from __future__ import annotations

from datetime import date, timedelta

from aiohttp import web

from ...core.keys import DB
from ...core.modules import ModuleContext, enabled
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.tenant import current_practice_id
from ...core.timeutil import day_bounds_utc, from_iso, today
from ...core.web import int_param, redirect_with_flash, render, form_int
from ..patients import api as patients_api
from . import api, repo


async def timeline(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    return await patients_api.file_page(request, patient, "timeline", "documentation/timeline.html", {
        "notes": repo.for_patient(db, patient["id"]), "series": patients_api.measurement_charts(db, patient["id"], w=320, h=72)})


async def note_new(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    patient = patients_api.require_patient(request, "pid")
    appointment_id = int(request.query["termin"]) if request.query.get("termin", "").isdigit() else None
    error = None
    if request.method == "POST":
        form = await request.post()
        sections = {s: form.get(s, "") for s in repo.SECTIONS}
        session_date = _session_date(form.get("session_date"))
        if session_date is None:
            error = "notes.date_invalid"
            return await render(request, "documentation/new.html", {
                "patient": patient, "tpl": {**sections, "id": None}, "appointment_id": appointment_id, "error": error,
                "templates": repo.templates(db, None, request["user"]["id"]), "today": today().isoformat()}, status=400)
        nid = repo.create(db, patient["id"], sections, appointment_id=appointment_id,
                          kind=form.get("kind") if form.get("kind") in ("session", "initial", "discharge") else "session",
                          session_date=session_date)
        if form.get("approve") == "1":
            await api.approve(request.app, nid)
            raise redirect_with_flash(f"{S}/patienten/{patient['id']}/verlauf", "notes.approved")
        raise redirect_with_flash(f"{S}/notizen/{nid}", "notes.saved")
    tpl = None
    if request.query.get("vorlage", "").isdigit():
        tpl = repo.template(db, int(request.query["vorlage"]), request["user"]["id"])
    return await render(request, "documentation/new.html", {"patient": patient, "tpl": tpl, "appointment_id": appointment_id,
                                                           "templates": repo.templates(db, None, request["user"]["id"]),
                                                           "today": today().isoformat(), "error": error})


def _session_date(value) -> str | None:
    """Datum der Sitzung aus dem Formular: leer = heute; sonst ein echtes Datum (JJJJ-MM-TT), nicht vor 1990 und höchstens
    morgen. Ungültig → None (Formular zeigt einen Fehler, nichts wird gespeichert)."""
    raw = str(value or "").strip()
    if not raw:
        return today().isoformat()
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None
    if len(raw) != 10 or d.year < 1990 or d > today() + timedelta(days=1):
        return None
    return d.isoformat()


def _load(request: web.Request):
    """Notiz laden – nur wenn die Akte für diese Person offen ist (eigene Patient/in oder Vertretung)."""
    db = request.app[DB]
    note = repo.get(db, int_param(request, "nid"))
    if note is None or not patients_api.can_open(request, note["patient_id"]):
        raise web.HTTPNotFound()
    return note


async def note_view(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    note = _load(request)
    patient = patients_api.require_patient(request, patient_id=note["patient_id"])
    return await render(request, "documentation/note.html", {"note": note, "patient": patient, "prefill": None,
                                                            "versions": repo.versions(db, note["id"]), "can_write": True})


async def note_save(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    note = _load(request)
    form = await request.post()
    sections = {s: form.get(s, "") for s in repo.SECTIONS}
    note = repo.get(db, note["id"]) or note                       # Stand nach dem Absenden
    try:
        base = int(str(form.get("base") or "0"))
    except ValueError:
        base = 0
    try:
        version = repo.save(db, note["id"], sections, form.get("reason"))
    except repo.NoteLocked:
        if form.get("reason") is None and base:
            # Entwurf war inzwischen freigegeben (anderes Fenster): Text nicht verlieren – als Nachtrag vorbereitet zeigen
            patient = patients_api.require_patient(request, patient_id=note["patient_id"])
            return await render(request, "documentation/note.html", {"note": note, "patient": patient, "prefill": sections,
                                                                    "versions": repo.versions(db, note["id"]), "can_write": True})
        raise redirect_with_flash(f"{S}/notizen/{note['id']}?nachtrag=1", "notes.reason_required")
    if base and note["version"] != base and version != note["version"]:
        # Inzwischen hat jemand (oder ein anderes Fenster) eine Version gespeichert: diese Fassung gilt jetzt, die andere bleibt im Verlauf
        raise redirect_with_flash(f"{S}/notizen/{note['id']}", "notes.saved_over_newer")
    if form.get("approve") == "1":
        await api.approve(request.app, note["id"])
        raise redirect_with_flash(f"{S}/patienten/{note['patient_id']}/verlauf", "notes.approved")
    raise redirect_with_flash(f"{S}/notizen/{note['id']}", "notes.saved" if note["status"] == "draft" else "notes.addendum_saved")


async def note_approve(request: web.Request) -> web.StreamResponse:
    note = _load(request)
    await api.approve(request.app, note["id"])
    raise redirect_with_flash(f"{prefix(request)}/patienten/{note['patient_id']}/verlauf", "notes.approved")


async def note_discard(request: web.Request) -> web.StreamResponse:
    from ...core.events import publish

    note = _load(request)
    if note["status"] == "draft":
        await publish(request.app, "note.discarding", note_id=note["id"], patient_id=note["patient_id"])
        repo.discard_draft(request.app[DB], note["id"])
    raise redirect_with_flash(f"{prefix(request)}/patienten/{note['patient_id']}/verlauf", "notes.discarded")


async def my_drafts(request: web.Request) -> web.StreamResponse:
    return await render(request, "documentation/drafts.html", {"notes": repo.drafts_for(request.app[DB], request["user"]["id"])})


# --------------------------------------------------------------------------- Vorlagen
def _template_values(form) -> dict:
    return {k: form.get(k, "") for k in ("name", "lang", *repo.SECTIONS)}


async def templates_page(request: web.Request) -> web.StreamResponse:
    """Therapie: eigene Vorlagen anlegen, ändern, löschen; Vorlagen der Praxis sehen (verwaltet die Leitung)."""
    db = request.app[DB]
    me = request["user"]["id"]
    back = f"{prefix(request)}/notizen/vorlagen"
    if request.method == "POST":
        form = await request.post()
        tid = form_int(form, "id")
        if form.get("do") == "delete" and tid:
            if not repo.delete_template(db, tid, owner_id=me):
                raise web.HTTPNotFound()
            raise redirect_with_flash(back, "notes.tpl_deleted")
        saved = repo.save_template(db, _template_values(form), owner_id=me, template_id=tid)
        if saved is None:
            if tid:
                raise web.HTTPNotFound()
            raise redirect_with_flash(back, "notes.tpl_name_missing")
        raise redirect_with_flash(back, "common.saved")
    edit = None
    if request.query.get("bearbeiten", "").isdigit():
        edit = repo.template(db, int(request.query["bearbeiten"]), me)
        if edit is None or edit["owner_id"] != me:
            raise web.HTTPNotFound()
    return await render(request, "documentation/templates.html", {
        "own": repo.templates(db, None, me, own_only=True), "shared": repo.templates(db, None, me, practice_only=True),
        "edit": edit, "langs": repo.TEMPLATE_LANGS, "action": back, "mode": "own"})


async def templates_settings(request: web.Request) -> web.StreamResponse:
    """Leitung › Einstellungen › Notizvorlagen: Vorlagen für die ganze Praxis."""
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/notizvorlagen"
    if request.method == "POST":
        form = await request.post()
        tid = form_int(form, "id")
        if form.get("do") == "delete" and tid:
            if not repo.delete_template(db, tid, owner_id=None):
                raise web.HTTPNotFound()
            raise redirect_with_flash(back, "notes.tpl_deleted")
        saved = repo.save_template(db, _template_values(form), owner_id=None, template_id=tid)
        if saved is None:
            if tid:
                raise web.HTTPNotFound()
            raise redirect_with_flash(back, "notes.tpl_name_missing")
        raise redirect_with_flash(back, "common.saved")
    edit = None
    if request.query.get("bearbeiten", "").isdigit():
        edit = repo.template(db, int(request.query["bearbeiten"]), None)
        if edit is None or edit["owner_id"] is not None:
            raise web.HTTPNotFound()
    return await settings_page(request, "notizvorlagen", "documentation/s_templates.html", {
        "own": [], "shared": repo.templates(db, None, None, practice_only=True), "edit": edit, "langs": repo.TEMPLATE_LANGS,
        "action": back, "mode": "practice"})


# --------------------------------------------------------------------------- Leitung: offene Arbeit (ohne Inhalte)
def _names(db, ids: set[int], table: str) -> dict[int, object]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    if table == "patients":
        rows = db.query(f"SELECT id, first_name, last_name FROM patients WHERE practice_id = ? AND id IN ({marks})",
                        (current_practice_id(), *ids))
    else:
        rows = db.query(f"SELECT id, name FROM users WHERE practice_id = ? AND id IN ({marks})", (current_practice_id(), *ids))
    return {r["id"]: r for r in rows}


def _without_note(request: web.Request) -> list[dict]:
    """Erledigte Termine (älter als 2 Tage, letzte 8 Wochen) ohne Notiz – nur Datum, Person, Therapeut/in."""
    if not enabled(request, "agenda"):
        return []
    from ..agenda import api as agenda_api

    db = request.app[DB]
    lo, _ = day_bounds_utc(today() - timedelta(weeks=8))
    hi, _ = day_bounds_utc(today() - timedelta(days=2))
    with_note = repo.appointment_ids_with_note(db)
    rows = [a for a in agenda_api.between(db, lo, hi, patients_only=True) if a["status"] == "done" and a["id"] not in with_note]
    patients = _names(db, {a["patient_id"] for a in rows}, "patients")
    users = _names(db, {a["therapist_id"] for a in rows}, "users")
    return [{"at": a["starts_at"], "start": from_iso(a["starts_at"]), "patient": patients.get(a["patient_id"]),
             "therapist": users[a["therapist_id"]]["name"] if a["therapist_id"] in users else "–"} for a in rows]


async def open_work_page(request: web.Request) -> web.StreamResponse:
    """Leitung: Notizen im Entwurf bzw. Sitzungen ohne Notiz – wer hat wie viele offen (keine Inhalte)."""
    art = "ohne-notiz" if request.query.get("art") == "ohne-notiz" else "entwuerfe"
    if art == "entwuerfe":
        rows = [{"at": n["session_date"], "start": None, "patient": n, "therapist": n["author_name"]}
                for n in repo.drafts_overview(request.app[DB])]
    else:
        rows = _without_note(request)
    per: dict[str, int] = {}
    for r in rows:
        per[r["therapist"]] = per.get(r["therapist"], 0) + 1
    return await render(request, "documentation/l_open.html", {
        "art": art, "rows": sorted(rows, key=lambda r: (r["therapist"], r["at"])), "per": sorted(per.items())})


# --------------------------------------------------------------------------- Steckplätze
async def card_last_note(request: web.Request, patient) -> dict | None:
    notes = repo.for_patient(request.app[DB], patient["id"], limit=1)
    return {"note": notes[0], "patient": patient} if notes else None


def tab_timeline(request: web.Request, patient) -> dict:
    n = request.app[DB].value("SELECT COUNT(*) FROM notes WHERE practice_id = ? AND patient_id = ? AND status = 'draft'",
                              (patient["practice_id"], patient["id"]), 0)
    return {"key": "timeline", "label_key": "notes.tab", "path": "verlauf", "count": n}


def action_new_note(request: web.Request, patient) -> dict:
    return {"label_key": "notes.new", "icon": "edit_note", "href": f"{prefix(request)}/patienten/{patient['id']}/notizen/neu",
            "icon_only": True}


def todo_drafts(request: web.Request) -> dict:
    return {"label_key": "today.open_drafts", "icon": "edit_note", "href": f"{prefix(request)}/notizen/entwuerfe",
            "n": repo.draft_count(request.app[DB], request["user"]["id"])}


def work_drafts(request: web.Request) -> dict:
    return {"label_key": "stats.open.notes_draft", "icon": "description", "n": repo.draft_count(request.app[DB]),
            "href": f"{prefix(request)}/offene-notizen?art=entwuerfe"}


def work_without_note(request: web.Request) -> dict | None:
    """Erledigte Termine (älter als 2 Tage, letzte 8 Wochen) ohne Notiz."""
    from ...core.modules import enabled
    from ...core.timeutil import day_bounds_utc, today

    if not enabled(request, "agenda"):
        return None
    from datetime import timedelta

    from ..agenda import api as agenda_api

    db = request.app[DB]
    lo, _ = day_bounds_utc(today() - timedelta(weeks=8))
    hi, _ = day_bounds_utc(today() - timedelta(days=2))
    with_note = repo.appointment_ids_with_note(db)
    n = sum(1 for a in agenda_api.between(db, lo, hi, patients_only=True) if a["status"] == "done" and a["id"] not in with_note)
    return {"label_key": "stats.open.done_without_note", "icon": "edit_note", "n": n,
            "href": f"{prefix(request)}/offene-notizen?art=ohne-notiz"}


def setup(ctx: ModuleContext) -> None:
    P = "/patienten/{pid:\\d+}"
    N = "/notizen/{nid:\\d+}"
    ctx.page("therapie", P + "/verlauf", timeline)
    ctx.page("therapie", P + "/notizen/neu", note_new, ("GET", "POST"))
    ctx.page("therapie", "/notizen/entwuerfe", my_drafts)
    ctx.page("therapie", N, note_view)
    ctx.page("therapie", N, note_save, ("POST",))
    ctx.page("therapie", N + "/freigeben", note_approve, ("POST",))
    ctx.page("therapie", N + "/verwerfen", note_discard, ("POST",))
    ctx.slot("therapie.patient.tab", 20, tab_timeline)
    ctx.slot("therapie.patient.center", 60, card_last_note, "documentation/card_last_note.html")
    ctx.slot("therapie.patient.action", 30, action_new_note)
    ctx.slot("therapie.today.todo", 30, todo_drafts)
    ctx.slot("leitung.overview.work", 10, work_drafts)
    ctx.slot("leitung.overview.work", 20, work_without_note)
    ctx.page("therapie", "/notizen/vorlagen", templates_page, ("GET", "POST"))
    ctx.page("leitung", "/einstellungen/notizvorlagen", templates_settings, ("GET", "POST"))
    ctx.slot("leitung.settings", 72, lambda request: {"key": "notizvorlagen", "label_key": "notes.tpl_title", "path": "notizvorlagen"})
    ctx.page("leitung", "/offene-notizen", open_work_page)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('notes',), (
        "UPDATE notes SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
