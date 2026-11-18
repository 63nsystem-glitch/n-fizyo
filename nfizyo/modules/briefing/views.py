"""Kurzüberblick (auf «Heute», bei jedem Termin, eigene Seite fürs Telefon) und Übergabe (Akte, eigene Seite)."""
from __future__ import annotations

from aiohttp import web

from ...core import patients as core_patients
from ...core.keys import DB
from ...core.modules import ModuleContext
from ...core.screens import prefix
from ...core.web import int_param, render
from ..agenda import api as agenda_api
from ..patients import api as patients_api
from . import data


def _patient_of(request: web.Request, appointment):
    if not appointment or not appointment["patient_id"] or not patients_api.can_open(request, appointment["patient_id"]):
        return None
    return core_patients.get(request.app[DB], appointment["patient_id"])


# --------------------------------------------------------------------------- Kurzüberblick
async def next_brief(request: web.Request, appointment) -> dict | None:
    """Im Feld «Als Nächstes» auf «Heute»."""
    patient = _patient_of(request, appointment)
    return data.briefing(request, patient, appointment) if patient else None


def action_brief(request: web.Request, appointment) -> dict | None:
    if appointment["status"] not in ("booked", "arrived"):
        return None
    return {"label_key": "brief.action", "icon": "fact_check", "href": f"{prefix(request)}/kurzinfo/{appointment['id']}",
            "primary": False}


async def brief_page(request: web.Request) -> web.StreamResponse:
    appointment = agenda_api.get(request.app[DB], int_param(request, "aid"))
    patient = _patient_of(request, appointment)
    if patient is None:
        raise web.HTTPNotFound()
    return await render(request, "briefing/page.html", {"b": data.briefing(request, patient, appointment)})


# --------------------------------------------------------------------------- Übergabe
async def card_handover(request: web.Request, patient) -> dict | None:
    return data.handover(request, patient)


async def handover_page(request: web.Request) -> web.StreamResponse:
    patient = patients_api.require_patient(request)
    return await patients_api.file_page(request, patient, "overview", "briefing/handover.html",
                                        {"h": data.handover(request, patient, full=True)})


async def job_translate_goal(app: web.Application, practice_id: int, patient_id: int) -> None:
    """Ziel der Patientin / des Patienten in die Sprachen der Praxis übersetzen (Zwischenspeicher, lokal). Nur Anzeige –
    das Original bleibt unverändert und sichtbar."""
    import asyncio

    from ...core.keys import AI
    from ...core.screens import roles_of

    db = app[DB]
    p = core_patients.get(db, patient_id)
    if p is None or not (p["goal"] or "").strip():
        return
    src = data.goal_lang(p)
    targets = {db.value("SELECT default_lang FROM practices WHERE id = ?", (practice_id,), "de")}
    targets |= {u["lang"] for u in db.query("SELECT lang, roles FROM users WHERE practice_id = ? AND active = 1", (practice_id,))
                if "therapist" in roles_of(u) and u["lang"] in ("de", "fr")}
    for target in sorted(targets - {src}):
        await asyncio.to_thread(app[AI].translate, db, practice_id, p["goal"], src, target)


def setup(ctx: ModuleContext) -> None:
    ctx.job("translate_goal", job_translate_goal)
    ctx.page("therapie", "/kurzinfo/{aid:\\d+}", brief_page)
    ctx.page("therapie", "/patienten/{pid:\\d+}/uebergabe", handover_page)
    ctx.slot("therapie.today.next", 10, next_brief, "briefing/next.html")
    ctx.slot("therapie.appointment.action", 5, action_brief)
    ctx.slot("therapie.patient.center", 5, card_handover, "briefing/card_handover.html")
