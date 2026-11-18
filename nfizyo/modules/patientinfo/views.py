"""Information für Patient/innen – gute Verständigung in der eigenen Sprache.

Ablauf:
  1. Die Therapeutin / der Therapeut diktiert die Sitzung (Modul «dictation»).
  2. Aus demselben Diktat schlägt die KI zwei kurze Teile vor: «Was Sie zu Hause tun können» und
     «Wann Sie sich melden sollten» (in der Sprache der Therapie, einfache Sprache, nichts erfunden).
  3. Beim Prüfen des Diktats sieht die Therapie den Vorschlag, ändert ihn und gibt ihn zusammen mit der Notiz frei.
  4. Erst dann wird lokal in die Sprache der Patientin / des Patienten übersetzt und in der App angezeigt –
     zum Lesen und zum Vorlesen lassen (Stimme des eigenen Telefons, nichts geht ins Internet).
Ohne Diktat kann die Therapie eine Information auch selbst schreiben (Reiter «Info» in der Akte).
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from ...core import languages
from ...core import patients as core_patients
from ...core.events import publish
from ...core.keys import AI, DB, JOBS
from ...core.modules import ModuleContext, NavItem, enabled
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.web import redirect_with_flash, render
from ..dictation import api as dictation_api
from ..patients import api as patients_api
from . import repo

MAX_LEN = 1500


def _clean(value) -> str:
    return str(value or "").strip()[:MAX_LEN]


def patient_lang(patient) -> str:
    """Sprache, in der die Patientin / der Patient liest: Sprache der App."""
    return patient["lang"] or "de"


# --------------------------------------------------------------------------- Ereignisse und Hintergrundaufgaben
def on_transcribed(app: web.Application, *, recording_id: int, patient_id: int, user_id: int, language: str, **_) -> None:
    """Diktat hat Text → Vorschlag für die Patienteninformation erzeugen lassen."""
    db = app[DB]
    info_id = repo.start(db, patient_id, recording_id, user_id, language or "de")
    if info_id:
        app[JOBS].enqueue(current_practice_id(), "patientinfo_draft", info_id)


async def job_draft(app: web.Application, practice_id: int, info_id: int) -> None:
    db = app[DB]
    info = repo.get(db, info_id)
    if info is None or info["status"] != "generating" or not info["recording_id"]:
        return
    found = dictation_api.transcript(db, info["recording_id"])
    if found is None:
        return
    text, lang = found
    result = await asyncio.to_thread(app[AI].patient_info, text, lang or info["source_lang"])
    repo.save_draft(db, info_id, result.home, result.contact, result.engine, result.warnings)


async def job_translate(app: web.Application, practice_id: int, info_id: int) -> None:
    db = app[DB]
    info = repo.get(db, info_id)
    if info is None or info["status"] != "approved":
        return
    src, target = info["source_lang"], info["target_lang"] or info["source_lang"]
    if src == target:
        repo.save_translation(db, info_id, info["home"], info["contact"], "identity", None)
        return
    out, engine, warning = {}, None, None
    for key in ("home", "contact"):
        if not info[key]:
            out[key] = None
            continue
        res = await asyncio.to_thread(app[AI].translate, db, practice_id, info[key], src, target)
        out[key] = res.text if res.text and res.engine not in ("identity", "none") else None
        engine = engine or res.engine
        warning = warning or res.warning
    repo.save_translation(db, info_id, out["home"], out["contact"], engine or "none", warning)


async def on_dictation_approved(app: web.Application, *, recording_id: int, patient_id: int, form=None, **_) -> None:
    """Diktat freigegeben → Patienteninformation mit freigeben (wenn angehakt) und übersetzen lassen."""
    db = app[DB]
    info = repo.by_recording(db, recording_id)
    if info is None or info["status"] == "approved":
        return
    home, contact = _clean(form.get("pi_home")) if form else "", _clean(form.get("pi_contact")) if form else ""
    if form is None or form.get("pi_send") != "1" or not (home or contact):
        repo.discard(db, info["id"])
        return
    patient = core_patients.get(db, patient_id)
    repo.approve(db, info["id"], home, contact, patient_lang(patient) if patient else "de")
    app[JOBS].enqueue(current_practice_id(), "patientinfo_translate", info["id"])
    await publish(app, "patientinfo.released", info_id=info["id"], patient_id=patient_id)


# --------------------------------------------------------------------------- Therapie
def review_card(request: web.Request, rec) -> dict | None:
    """Karte beim Prüfen des Diktats (liegt im selben Formular wie die Notiz)."""
    info = repo.by_recording(request.app[DB], rec["id"])
    if info is None or info["status"] == "discarded":
        return None
    patient = core_patients.get(request.app[DB], rec["patient_id"])
    target = patient_lang(patient)
    return {"info": info, "warnings": json.loads(info["ai_warnings"] or "[]"), "target_name": languages.name_for(target, request["lang"]),
            "foreign": target != request["lang"], "pending": info["status"] == "generating",
            "translate_on": request.app[AI].can_translate()}


async def patient_tab_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    if request.method == "POST":
        form = await request.post()
        home, contact = _clean(form.get("home")), _clean(form.get("contact"))
        if home or contact:
            iid = repo.create_approved(db, patient["id"], home, contact, request["lang"], patient_lang(patient))
            request.app[JOBS].enqueue(current_practice_id(), "patientinfo_translate", iid)
            await publish(request.app, "patientinfo.released", info_id=iid, patient_id=patient["id"])
            raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}/info", "patientinfo.sent")
        raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}/info", "patientinfo.empty")
    infos = repo.for_patient(db, patient["id"])
    notified = {}
    if enabled(request, "notify"):
        from ..notify import api as notify_api

        notified = notify_api.status_for(db, "info", [i["id"] for i in infos if i["status"] == "approved"])
    return await patients_api.file_page(request, patient, "info", "patientinfo/t_patient.html", {
        "infos": infos, "notified": notified,
        "target_name": languages.name_for(patient_lang(patient), request["lang"]),
        "foreign": patient_lang(patient) != request["lang"], "translate_on": request.app[AI].can_translate(),
    })


async def draft_status(request: web.Request) -> web.StreamResponse:
    """Für die Prüfseite: Ist der Vorschlag fertig? (füllt die Felder, ohne die Seite neu zu laden)"""
    from ...core.web import int_param

    info = repo.get(request.app[DB], int_param(request, "iid"))
    if info is None or not patients_api.can_open(request, info["patient_id"]):
        raise web.HTTPNotFound()
    return web.json_response({"done": info["status"] != "generating", "home": info["home"] or "", "contact": info["contact"] or ""})


def tab_info(request: web.Request, patient) -> dict:
    return {"key": "info", "label_key": "patientinfo.tab", "path": "info", "count": 0}


def card_last(request: web.Request, patient) -> dict | None:
    infos = repo.for_patient(request.app[DB], patient["id"], approved_only=True, limit=1)
    return {"info": infos[0], "patient": patient} if infos else None


# --------------------------------------------------------------------------- Patienten-App
async def p_list(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    p = request["patient"]
    infos = repo.for_patient(db, p["id"], approved_only=True, limit=20)
    unread = {i["id"] for i in infos if not i["read_at"]}
    repo.mark_read(db, p["id"])
    return await render(request, "patientinfo/p_list.html", {"infos": infos, "unread": unread, "plang": patient_lang(p)})


def p_card(request: web.Request) -> dict | None:
    db = request.app[DB]
    p = request["patient"]
    infos = repo.for_patient(db, p["id"], approved_only=True, limit=1)
    if not infos:
        return None
    return {"info": infos[0], "unread": repo.unread_count(db, p["id"])}


def badge_unread(db, practice_id: int, patient) -> int:
    with practice_scope(practice_id):
        return repo.unread_count(db, patient["id"])


def setup(ctx: ModuleContext) -> None:
    ctx.on("dictation.transcribed", on_transcribed)
    ctx.on("dictation.approved", on_dictation_approved)
    ctx.job("patientinfo_draft", job_draft)
    ctx.job("patientinfo_translate", job_translate)
    ctx.slot("therapie.dictation.review", 10, review_card, "patientinfo/review_card.html")
    ctx.page("therapie", "/patienten/{pid:\\d+}/info", patient_tab_page, ("GET", "POST"))
    ctx.page("therapie", "/patienteninfo/{iid:\\d+}/status", draft_status)
    ctx.slot("therapie.patient.tab", 55, tab_info)
    ctx.slot("therapie.patient.right", 25, card_last, "patientinfo/card_last.html")
    ctx.patient_page("/info", p_list)
    ctx.nav("patient", NavItem("p.nav.info", "info", "/info", order=25, badge="patientinfo_unread"))
    ctx.badge("patientinfo_unread", badge_unread)
    ctx.slot("patient.home.card", 15, p_card, "patientinfo/p_card.html")
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('patient_infos',), (
        "UPDATE patient_infos SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
