"""Anliegen in der eigenen Sprache: Patient/in beschreibt zu Hause in der App (Körperkarte, Schmerz, Text oder Sprache),
die KI übersetzt lokal, die Therapie sieht es in der Akte und übernimmt es auf Wunsch als Befund."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiohttp import web

from ...ai.stt import STTRejected, STTUnavailable
from ...core import access, languages
from ...core.keys import AI, DB, I18N, JOBS
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.web import int_param, redirect_with_flash, render
from ...knowledge.regions import FACES
from ..patients import api as patients_api
from . import repo

log = logging.getLogger(__name__)


def form_context(stt: bool) -> dict:
    return {"faces": FACES, "since_opts": repo.SINCE, "worse_opts": repo.WORSE, "goal_opts": repo.GOALS,
            "region_bases": repo.REGION_BASES, "stt": stt}


async def _save(request: web.Request, patient_id: int, channel: str, lang: str) -> int:
    db = request.app[DB]
    form = await request.post()
    pain = form.get("pain")
    iid = repo.create(
        db, patient_id, channel=channel, lang=lang, regions=form.getall("region", []),
        pain=int(pain) if str(pain or "").isdigit() else None, since=form.get("since"),
        worse=form.getall("worse", []), goals=form.getall("goal", []), text=str(form.get("text") or ""),
    )
    if form.get("text"):
        request.app[JOBS].enqueue(current_practice_id(), "translate_intake", iid)
    return iid


async def job_translate(app: web.Application, practice_id: int, iid: int) -> None:
    db = app[DB]
    it = repo.get(db, iid)
    if it is None or not it["text_original"]:
        return
    target = db.value("SELECT default_lang FROM practices WHERE id = ?", (practice_id,), "de")
    if it["lang"] == target:
        repo.set_translation(db, iid, it["text_original"], target, "identity", None)
        return
    result = await asyncio.to_thread(app[AI].translate, db, practice_id, it["text_original"], it["lang"], target)
    if result.text:
        repo.set_translation(db, iid, result.text, target, result.engine, result.warning)


MAX_AUDIO = 20 * 1024 * 1024        # Sprachaufnahme fürs Anliegen: wenige Minuten sind < 3 MB
MAX_PER_DAY = 5                     # Anliegen pro Person und Tag aus der App (Übersetzungen dürfen Diktate nicht aufhalten)


class _TooLarge(Exception):
    pass


async def _transcribe(request: web.Request, lang: str) -> web.StreamResponse:
    """Sprachaufnahme der Patientin / des Patienten → Text in der eigenen Sprache (lokal)."""
    ai = request.app[AI]
    if not await asyncio.to_thread(ai.can_transcribe):
        return web.json_response({"error": "stt_unavailable"}, status=503)
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "audio":
        return web.json_response({"error": "missing"}, status=400)
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as fh:
        path = Path(fh.name)
        size = 0
        try:
            while chunk := await part.read_chunk(256 * 1024):
                size += len(chunk)
                if size > MAX_AUDIO:       # ohne Anmeldung der Praxis erreichbar: nie die Platte füllen lassen
                    raise _TooLarge()
                fh.write(chunk)
        except _TooLarge:
            path.unlink(missing_ok=True)
            return web.json_response({"error": "too_large"}, status=413)
        except BaseException:
            path.unlink(missing_ok=True)   # Verbindung abgebrochen: keine halbe Sprachaufnahme liegen lassen
            raise
    try:
        t = await asyncio.to_thread(ai.transcribe, path, languages.whisper_code(lang))
    except STTUnavailable:
        return web.json_response({"error": "stt_unavailable"}, status=503)
    except STTRejected:
        return web.json_response({"error": "unreadable"}, status=422)
    finally:
        path.unlink(missing_ok=True)  # Audio wird nicht gespeichert
    return web.json_response({"text": t.text})


# --------------------------------------------------------------------------- Patienten-App
async def app_form(request: web.Request) -> web.StreamResponse:
    patient = request["patient"]
    if request.method == "POST":
        if repo.count_since(request.app[DB], patient["id"], hours=24) >= MAX_PER_DAY:
            raise redirect_with_flash("/p/heute", "p.msg.too_many")
        await _save(request, patient["id"], "app", patient["spoken_lang"] if patient["spoken_lang"] in languages.BY_CODE else request["lang"])
        raise redirect_with_flash("/p/heute", "intake.sent_flash")
    ctx = form_context(request.app[AI].can_transcribe())
    src = patient["spoken_lang"] if patient["spoken_lang"] in languages.BY_CODE else request["lang"]
    main = request.app[DB].value("SELECT default_lang FROM practices WHERE id = ?", (current_practice_id(),), "de") or "de"
    ctx.update({"layout": "layouts/patient.html", "action": "/p/anliegen", "voice_url": "/p/anliegen/aufnahme",
                "translated": src != main})                       # Hinweis «wird übersetzt» nur, wenn wirklich übersetzt wird
    return await render(request, "intake/form.html", ctx)


async def app_voice(request: web.Request) -> web.StreamResponse:
    p = request["patient"]
    return await _transcribe(request, p["spoken_lang"] or request["lang"])


def p_card(request: web.Request) -> dict:
    return {"show": True}


# --------------------------------------------------------------------------- Therapie-Ansicht
FRONT_ONLY = {"abs", "biceps", "chest", "front_deltoids", "neck", "obliques", "quadriceps"}
BACK_ONLY = {"back_deltoids", "gluteal", "hamstring", "lower_back", "soleus", "trapezius", "upper_back"}


def present(it, lang: str) -> dict:
    regions = repo.split(it["regions"])
    bases = {r[:-2] for r in regions if r != "head"}
    return {
        "fig_view": "back" if bases & BACK_ONLY and not bases & FRONT_ONLY else "front",
        "it": it,
        "regions": regions,
        "marks": {"muscle": {r: "hl-1" for r in regions}, "skeleton": {}},
        "worse": repo.split(it["worse"]),
        "goals": repo.split(it["goals"]),
        "lang_name": languages.name_for(it["lang"], lang),
        "foreign": it["lang"] not in ("de", "fr", "gsw"),
    }


async def card_intake(request: web.Request, patient) -> dict | None:
    items = repo.for_patient(request.app[DB], patient["id"], limit=3)
    current = [i for i in items if i["status"] == "new"] or items[:1]
    if not current:
        return None
    return {**present(current[0], request["lang"]), "patient": patient, "translate_on": request.app[AI].can_translate()}


async def intake_list(request: web.Request) -> web.StreamResponse:
    rows = [present(i, request["lang"]) for i in repo.listing(request.app[DB], only_new=request.query.get("alle") != "1",
                                                              only=access.patient_ids_sql(request["user"]["id"]))]
    return await render(request, "intake/list.html", {"rows": rows, "all": request.query.get("alle") == "1",
                                                      "translate_on": request.app[AI].can_translate()})


def _load(request: web.Request):
    it = repo.get(request.app[DB], int_param(request, "iid"))
    if it is None or not patients_api.can_open(request, it["patient_id"]):
        raise web.HTTPNotFound()
    return it


async def intake_seen(request: web.Request) -> web.StreamResponse:
    S = prefix(request)
    it = _load(request)
    repo.mark(request.app[DB], it["id"], "seen")
    form = await request.post()
    back = str(form.get("back") or f"{S}/patienten/{it['patient_id']}")
    raise redirect_with_flash(back if back.startswith(S + "/") else f"{S}/anliegen", "intake_staff.marked_seen")


async def intake_take(request: web.Request) -> web.StreamResponse:
    """Als Befund übernehmen: je markierte Körperstelle ein Schmerz-Befund mit Stärke."""
    db = request.app[DB]
    it = _load(request)
    lang = request["lang"]
    t = request.app[I18N].t
    detail_parts = []
    if it["since"]:
        detail_parts.append(t(lang, "intake.since") + ": " + t(lang, f"intake.since_opt.{it['since']}"))
    if it["worse"]:
        detail_parts.append(t(lang, "intake.worse") + ": " + ", ".join(t(lang, f"intake.worse_opt.{w}") for w in repo.split(it["worse"])))
    if it["text_translated"] or it["text_original"]:
        detail_parts.append((it["text_translated"] or it["text_original"]))
    for region in repo.split(it["regions"]) or [None]:
        base, side = (region[:-2], region[-1]) if region and region != "head" else (region, None)
        structure = repo.REGION_STRUCTURE.get(base or "")
        title = t(lang, "intake_staff.pain_at", place=(t(lang, f"common.region.{base}") + (" " + t(lang, f"common.side.{side}") if side else "")) if base else t(lang, "intake_staff.unspecified"))
        patients_api.add_finding(db, it["patient_id"], kind="pain", title=title, detail="\n".join(detail_parts) or None,
                                  structure_id=structure, region=region, side=side, severity=it["pain"], source="intake")
    repo.mark(db, it["id"], "taken")
    raise redirect_with_flash(f"{prefix(request)}/patienten/{it['patient_id']}", "intake_staff.taken")


def badge_new(db, practice_id: int, user) -> int:
    with practice_scope(practice_id):
        return len(repo.listing(db, only_new=True, only=access.patient_ids_sql(user["id"])))


def todo_intake(request: web.Request) -> dict:
    return {"label_key": "today.open_intake", "icon": "record_voice_over", "href": f"{prefix(request)}/anliegen",
            "n": badge_new(request.app[DB], request["user"]["practice_id"], request["user"])}


def usage_month(request: web.Request) -> dict:
    from ...core.timeutil import day_bounds_utc, today

    lo, _ = day_bounds_utc(today().replace(day=1))
    n = request.app[DB].value("SELECT COUNT(*) FROM intakes WHERE practice_id = ? AND created_at >= ?",
                              (current_practice_id(), lo), 0)
    return {"label_key": "stats.app.intakes", "n": n}


def setup(ctx: ModuleContext) -> None:
    ctx.patient_page("/anliegen", app_form, ("GET", "POST"))
    ctx.patient_page("/anliegen/aufnahme", app_voice, ("POST",))
    ctx.slot("patient.home.card", 30, p_card, "intake/p_card.html")
    ctx.page("therapie", "/anliegen", intake_list)
    ctx.page("therapie", "/anliegen/{iid:\\d+}/gesehen", intake_seen, ("POST",))
    ctx.page("therapie", "/anliegen/{iid:\\d+}/uebernehmen", intake_take, ("POST",))
    ctx.nav("therapie", NavItem("nav.intake", "record_voice_over", "/anliegen", order=45, badge="intake_new"))
    ctx.badge("intake_new", badge_new)
    ctx.slot("therapie.patient.center", 10, card_intake, "intake/card_intake.html")
    ctx.slot("therapie.today.todo", 25, todo_intake)
    ctx.job("translate_intake", job_translate)
    ctx.slot("leitung.overview.usage", 20, usage_month)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('intakes',), (
        "UPDATE intakes SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
