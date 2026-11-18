"""Nachrichten und "Problem melden" – mit lokaler Übersetzung in beide Richtungen."""
from __future__ import annotations

import asyncio

from aiohttp import web

from ...core import access, languages
from ...core.events import publish
from ...core.keys import AI, DB, I18N, JOBS
from ...core.modules import ModuleContext, NavItem, enabled
from ...core.practice import REPLY_TIMES
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.timeutil import from_iso, today
from ...core.web import redirect_with_flash, render
from ...knowledge import regions
from ...knowledge.regions import FACES
from ..patients import api as patients_api
from . import repo


def patient_language(p) -> str:
    """Sprache, in der die Patientin / der Patient liest (App-Sprache)."""
    return p["lang"] or "de"


def patient_writes(p, ui_lang: str) -> str:
    """Sprache, in der die Patientin / der Patient schreibt: gesprochene Sprache, falls bekannt."""
    return p["spoken_lang"] if p["spoken_lang"] in languages.BY_CODE and p["spoken_lang"] != "gsw" else ui_lang


async def job_translate(app: web.Application, practice_id: int, mid: int) -> None:
    db = app[DB]
    m = repo.last(db, mid)
    if m is None or m["direction"] != "in" or m["translation"]:
        return
    target = db.value("SELECT default_lang FROM practices WHERE id = ?", (practice_id,), "de")
    if m["lang"] in (target, "gsw"):
        return
    result = await asyncio.to_thread(app[AI].translate, db, practice_id, m["body"], m["lang"], target)
    if result.text:
        repo.set_translation(db, mid, result.text, target, result.engine, result.warning)


def reply_time_text(request: web.Request) -> str:
    """Antwortzeit der Praxis in der Sprache der App (Auswahl unter Leitung › Praxis); sonst der feste Standardtext."""
    t = request.app[I18N].t
    lang = request["lang"]
    choice = request.app[DB].value("SELECT reply_time FROM practices WHERE id = ?", (current_practice_id(),))
    if choice in REPLY_TIMES:
        return f"{t(lang, 'p.msg.reply.' + choice)} {t(lang, 'p.msg.no_emergency')}"
    return t(lang, "p.msg.reply_time")


def _preview(th, staff_lang: str) -> str:
    """Kurzvorschau der letzten Nachricht (Übersetzung, wenn vorhanden)."""
    if th["last_direction"] == "in" and th["last_translation"] and th["last_lang"] != staff_lang:
        text = th["last_translation"]
    else:
        text = th["last_body"] or ""
    return " ".join(text.split())


def _when(value: str | None) -> dict:
    dt = from_iso(value)
    if dt is None:
        return {"kind": "none"}
    d = dt.date()
    if d == today():
        return {"kind": "time", "at": value}
    if (today() - d).days == 1:
        return {"kind": "yesterday", "at": value}
    return {"kind": "date", "at": value, "year": d.year != today().year}


# --------------------------------------------------------------------------- Therapie
async def inbox(request: web.Request) -> web.StreamResponse:
    rows = []
    for th in repo.threads(request.app[DB], access.patient_ids_sql(request["user"]["id"])):
        rows.append(dict(th) | {"preview": _preview(th, request["lang"]), "when": _when(th["last_at"]),
                                "problem_only": th["last_kind"] == "problem" and (th["last_body"] or "–") == "–"})
    return await render(request, "messages/threads.html", {"threads": rows})


def _notify_status(request: web.Request, msgs) -> dict:
    """Stand der Benachrichtigung je Antwort der Praxis (wenn das Modul «Benachrichtigungen» eingeschaltet ist)."""
    if not enabled(request, "notify"):
        return {}
    from ..notify import api as notify_api

    return notify_api.status_for(request.app[DB], "message", [m["id"] for m in msgs if m["direction"] == "out"])


async def thread(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    patient = patients_api.require_patient(request)
    ai = request.app[AI]
    if request.method == "POST":
        form = await request.post()
        body = str(form.get("body") or "").strip()
        if form.get("handled", "").isdigit():
            repo.handle(db, int(form["handled"]))
            raise redirect_with_flash(f"{S}/nachrichten/{patient['id']}", "messages.handled")
        if body:
            staff_lang = request["lang"]
            target = patient_language(patient)
            translation = engine = warning = None
            if form.get("translate") == "1" and target != staff_lang and ai.can_translate():
                result = await asyncio.to_thread(ai.translate, db, current_practice_id(), body, staff_lang, target)
                translation, engine, warning = result.text, result.engine, result.warning
            mid = repo.add(db, patient["id"], direction="out", body=body, lang=staff_lang, translation=translation,
                           translation_lang=target if translation else None, translation_engine=engine, translation_warning=warning)
            await publish(request.app, "message.sent", message_id=mid, patient_id=patient["id"])
            raise redirect_with_flash(f"{S}/nachrichten/{patient['id']}", "messages.sent")
    repo.mark_read(db, patient["id"], "in")
    msgs = repo.thread(db, patient["id"])
    return await render(request, "messages/thread.html", {
        "patient": patient, "msgs": msgs, "can_translate": ai.can_translate(), "notified": _notify_status(request, msgs),
        "reads": languages.name_for(patient_language(patient), request["lang"]) if patient_language(patient) != request["lang"] else None,
        "lang_name": lambda code: languages.name_for(code, request["lang"]),
    })


async def card_message(request: web.Request, patient) -> dict | None:
    db = request.app[DB]
    m = db.one("SELECT * FROM messages WHERE practice_id = ? AND patient_id = ? AND direction = 'in'"
               " AND (read_at IS NULL OR (kind = 'problem' AND handled_at IS NULL)) ORDER BY id DESC LIMIT 1",
               (current_practice_id(), patient["id"]))
    if m is None:
        return None
    return {"m": m, "patient": patient, "lang_name": languages.name_for(m["lang"], request["lang"]),
            "foreign": m["lang"] not in ("de", "fr", "gsw")}


def badge_unread(db, practice_id: int, user) -> int:
    """Gespräche mit ungelesenen Nachrichten – nur eigene Patient/innen (und Vertretungen)."""
    with practice_scope(practice_id):
        return repo.unread_threads(db, access.patient_ids_sql(user["id"]))


def tab_messages(request: web.Request, patient) -> dict:
    n = request.app[DB].value("SELECT COUNT(*) FROM messages WHERE practice_id = ? AND patient_id = ? AND direction = 'in'"
                              " AND read_at IS NULL", (patient["practice_id"], patient["id"]), 0)
    return {"key": "messages", "label_key": "messages.tab", "href": f"{prefix(request)}/nachrichten/{patient['id']}", "count": n}


def todo_messages(request: web.Request) -> dict:
    return {"label_key": "today.open_messages", "icon": "forum", "href": f"{prefix(request)}/nachrichten",
            "n": badge_unread(request.app[DB], request["user"]["practice_id"], request["user"])}


# --------------------------------------------------------------------------- Patient
async def p_thread(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    p = request["patient"]
    if request.method == "POST":
        form = await request.post()
        body = str(form.get("body") or "").strip()[:repo.MAX_BODY]
        if body and repo.too_many(db, p["id"]):
            raise redirect_with_flash("/p/nachrichten", "p.msg.too_many")
        if body:
            mid = repo.add(db, p["id"], direction="in", body=body, lang=patient_writes(p, request["lang"]))
            request.app[JOBS].enqueue(current_practice_id(), "translate_message", mid)
            raise redirect_with_flash("/p/nachrichten", "p.msg.sent")
    repo.mark_read(db, p["id"], "out")
    return await render(request, "messages/p_thread.html", {"msgs": repo.thread(db, p["id"]), "reply_time": reply_time_text(request)})


async def p_report(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    p = request["patient"]
    if request.method == "POST":
        form = await request.post()
        body = str(form.get("text") or "").strip()[:repo.MAX_BODY]
        picked = [r for r in form.getall("region", []) if regions.valid_region(r)][:20]
        pain = int(form["pain"]) if str(form.get("pain", "")).isdigit() and int(form["pain"]) <= 10 else None
        if (body or picked or pain is not None) and repo.too_many(db, p["id"]):
            raise redirect_with_flash("/p/heute", "p.msg.too_many")
        if body or picked or pain is not None:
            mid = repo.add(db, p["id"], direction="in", kind="problem", body=body or "–", lang=patient_writes(p, request["lang"]),
                           region=",".join(picked) or None, pain=pain)
            if body:
                request.app[JOBS].enqueue(current_practice_id(), "translate_message", mid)
            raise redirect_with_flash("/p/heute", "p.msg.report_sent")
    return await render(request, "messages/p_report.html", {"faces": FACES, "region_bases": regions.REGION_BASES,
                                                            "reply_time": reply_time_text(request)})


def p_card(request: web.Request) -> dict:
    db = request.app[DB]
    unread = db.value("SELECT COUNT(*) FROM messages WHERE practice_id = ? AND patient_id = ? AND direction = 'out'"
                      " AND read_at IS NULL", (current_practice_id(), request["patient"]["id"]), 0)
    return {"unread": unread}


def badge_patient(db, practice_id: int, patient) -> int:
    return db.value("SELECT COUNT(*) FROM messages WHERE practice_id = ? AND patient_id = ? AND direction = 'out' AND read_at IS NULL",
                    (practice_id, patient["id"]), 0)


def usage_month(request: web.Request) -> dict:
    from ...core.timeutil import day_bounds_utc, today

    lo, _ = day_bounds_utc(today().replace(day=1))
    n = request.app[DB].value("SELECT COUNT(*) FROM messages WHERE practice_id = ? AND direction = 'in' AND created_at >= ?",
                              (current_practice_id(), lo), 0)
    return {"label_key": "stats.app.messages", "n": n}


def setup(ctx: ModuleContext) -> None:
    ctx.page("therapie", "/nachrichten", inbox)
    ctx.page("therapie", "/nachrichten/{pid:\\d+}", thread, ("GET", "POST"))
    ctx.nav("therapie", NavItem("nav.messages", "forum", "/nachrichten", order=50, badge="messages_unread", phone=True, short_key="nav.messages_short"))
    ctx.badge("messages_unread", badge_unread)
    ctx.slot("therapie.patient.center", 15, card_message, "messages/card_message.html")
    ctx.slot("therapie.patient.tab", 70, tab_messages)
    ctx.slot("therapie.today.todo", 15, todo_messages)
    ctx.job("translate_message", job_translate)
    ctx.patient_page("/nachrichten", p_thread, ("GET", "POST"))
    ctx.patient_page("/melden", p_report, ("GET", "POST"))
    ctx.nav("patient", NavItem("p.nav.messages", "forum", "/nachrichten", order=40, badge="messages_patient", short_key="p.nav.messages_short"))
    ctx.badge("messages_patient", badge_patient)
    ctx.slot("patient.home.card", 40, p_card, "messages/p_card.html")
    ctx.slot("leitung.overview.usage", 30, usage_month)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('messages',), (
        "UPDATE messages SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
