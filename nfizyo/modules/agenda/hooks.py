"""Was die Agenda in andere Bildschirmteile einsteckt (Karten, Reiter, Kacheln), Ereignisse und Erinnerungen."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from aiohttp import web

from ...core.keys import DB, I18N, MAIL, SMS
from ...core.modules import ModuleContext, NavItem
from ...core.sms import is_landline
from ...core.tenant import practice_scope
from ...core.timeutil import day_bounds_utc, fmt_date, fmt_time, from_iso, now_local
from ...core.web import render
from ..patients import api as patients_api
from . import absences, repo

log = logging.getLogger(__name__)


def _label(p) -> str:
    bd = fmt_date(p["birthdate"]) if p["birthdate"] else ""
    return f"{p['last_name']}, {p['first_name']} · {bd} · {p['number']}"


# --------------------------------------------------------------------------- Therapie: Akte
async def card_patient(request: web.Request, patient) -> dict:
    db = request.app[DB]
    a = repo.today_for_patient(db, patient["id"])
    nxt = repo.next_for_patient(db, patient["id"])
    session = repo.session_number(db, a) if a else None
    return {"a": a, "start": from_iso(a["starts_at"]) if a else None, "session": session, "beyond": repo.beyond_prescription(session),
            "next": nxt, "next_start": from_iso(nxt["starts_at"]) if nxt else None, "patient": patient, "label": _label(patient)}


def fact_next(request: web.Request, patient) -> str | None:
    nxt = repo.next_for_patient(request.app[DB], patient["id"])
    if nxt is None:
        return None
    s = from_iso(nxt["starts_at"])
    return request.app[I18N].t(request["lang"], "agenda.next_short",
                               when=f"{fmt_date(s, request['lang'], weekday=True, year=False)} {fmt_time(s)}")


def tab_appointments(request: web.Request, patient) -> dict:
    n = len(repo.for_patient(request.app[DB], patient["id"], future_only=True, limit=50))
    return {"key": "appointments", "label_key": "agenda.tab", "path": "termine", "count": n}


def _rows(db, patient_id: int):
    appts = repo.for_patient(db, patient_id, limit=200)
    sessions = repo.session_numbers(db, appts)
    return [{"a": a, "start": from_iso(a["starts_at"]), "end": from_iso(a["ends_at"]), "session": sessions.get(a["id"]),
             "beyond": repo.beyond_prescription(sessions.get(a["id"])) and a["status"] not in ("cancelled", "no_show")}
            for a in appts]


async def patient_appointments_t(request: web.Request) -> web.StreamResponse:
    patient = patients_api.require_patient(request)
    rows = _rows(request.app[DB], patient["id"])
    return await patients_api.file_page(request, patient, "appointments", "agenda/t_patient.html",
                                        {"rows": rows, "label": _label(patient), "n_beyond": sum(1 for r in rows if r["beyond"])})


# --------------------------------------------------------------------------- Empfang: Stammblatt
async def card_empfang(request: web.Request, patient) -> dict:
    db = request.app[DB]
    upcoming = [{"a": a, "start": from_iso(a["starts_at"])} for a in repo.for_patient(db, patient["id"], future_only=True, limit=5)]
    return {"patient": patient, "upcoming": upcoming, "label": _label(patient)}


async def patient_appointments_r(request: web.Request) -> web.StreamResponse:
    patient = patients_api.require_patient(request)
    rows = _rows(request.app[DB], patient["id"])
    skipped = []
    for value in str(request.query.get("uebersprungen") or "").split(",")[:40]:
        try:
            skipped.append(fmt_date(date.fromisoformat(value), request["lang"], weekday=True))
        except ValueError:
            continue
    return await render(request, "agenda/r_patient.html", {"patient": patient, "rows": rows, "label": _label(patient),
                                                           "skipped": skipped, "n_beyond": sum(1 for r in rows if r["beyond"])})


# --------------------------------------------------------------------------- Leitung: Übersicht
def tile_today(request: web.Request) -> dict:
    db = request.app[DB]
    start, end = day_bounds_utc(now_local().date())
    rows = repo.simple_between(db, start, end, patients_only=True)
    active = [r for r in rows if r["status"] in ("booked", "arrived", "done")]
    return {"label_key": "agenda.tile_today", "value": len(active), "icon": "event_available",
            "sub_key": "agenda.tile_today_sub", "sub_args": {"n": sum(1 for r in rows if r["status"] in ("cancelled", "no_show"))}}


# --------------------------------------------------------------------------- Patienten-App
def p_card_next(request: web.Request) -> dict | None:
    nxt = repo.next_for_patient(request.app[DB], request["patient"]["id"])
    return {"a": nxt, "start": from_iso(nxt["starts_at"])} if nxt else None


async def p_appointments(request: web.Request) -> web.StreamResponse:
    from ...core.audit import audit
    from ...core.web import redirect_with_flash

    db = request.app[DB]
    p = request["patient"]
    if request.method == "POST":
        form = await request.post()
        wish = str(form.get("wish") or "").strip()[:300]
        repo.add_waitlist(db, patient_id=p["id"], wish=wish or None, preferred=str(form.get("preferred") or "").strip()[:120] or None,
                          source="app")
        audit(db, "create", "appointment", None, p["id"], {"request": True})
        raise redirect_with_flash("/p/termine", "p.appt.requested")
    rows = [{"a": a, "start": from_iso(a["starts_at"])} for a in repo.for_patient(db, p["id"], future_only=True, limit=20)]
    return await render(request, "agenda/p_appointments.html", {"rows": rows, "closures": absences.closures_ahead(db, now_local().date())})


# --------------------------------------------------------------------------- Zähler, Ereignisse
def badge_waitlist(db, practice_id: int, user) -> int:
    return len(repo.waitlist(db))


def on_note_approved(app: web.Application, *, appointment_id: int | None = None, **_) -> None:
    """Notiz zum Termin freigegeben → Termin ist erledigt."""
    if not appointment_id:
        return
    db = app[DB]
    a = repo.get(db, appointment_id)
    if a is not None and a["status"] in ("booked", "arrived"):
        repo.set_status(db, appointment_id, "done")


# --------------------------------------------------------------------------- Erinnerungen (Hintergrund)
QUIET_FROM, QUIET_UNTIL = 21, 7     # keine Erinnerungen zwischen 21 und 7 Uhr (Zürcher Zeit)


def quiet_hours_over(hour: int) -> bool:
    """Nachtruhe: z. B. online um 23:30 für morgen gebucht → Erinnerung erst ab 7 Uhr, nicht mitten in der Nacht."""
    return QUIET_UNTIL <= hour < QUIET_FROM


def send_reminders(app: web.Application) -> None:
    """Erinnerungen für Termine in den nächsten 24 Stunden – je nach Wunsch der Patientin / des Patienten per SMS und/oder E-Mail."""
    db = app[DB]
    for practice in db.query("SELECT id FROM practices"):
        with practice_scope(practice["id"]):
            pr = db.one("SELECT name, phone FROM practices WHERE id = ?", (practice["id"],))
            for a in repo.due_reminders(db, practice["id"]):
                p = db.one("SELECT lang, first_name, phone, email, email_ok_at, reminder_channel FROM patients"
                           " WHERE id = ? AND practice_id = ?", (a["patient_id"], practice["id"]))
                if p is None:
                    continue
                s = from_iso(a["starts_at"])
                lang = p["lang"] or "de"
                when = f"{fmt_date(s, lang, weekday=True, year=False)} {fmt_time(s)}"
                channel = p["reminder_channel"] or "sms"
                if channel == "sms" and is_landline(p["phone"]):
                    # Festnetz: SMS käme nie an – mit Einverständnis per E-Mail erinnern, sonst gar nicht (keine Kosten)
                    channel = "email"
                if channel in ("sms", "both") and p["phone"] and not is_landline(p["phone"]):
                    app[SMS].send(practice["id"], "sms", p["phone"], app[I18N].t(lang, "p.sms.reminder", name=p["first_name"],
                                                                                 when=when, place=a["location_name"] or ""))
                if channel in ("email", "both") and p["email"] and p["email_ok_at"]:
                    params = {"name": p["first_name"], "when": when, "place": a["location_name"] or pr["name"],
                              "phone": pr["phone"] or "", "practice": pr["name"]}
                    body = app[I18N].t(lang, "p.mail.reminder_body", **params) + "\n\n" + app[I18N].t(lang, "p.mail.footer", **params)
                    app[MAIL].send(practice["id"], p["email"], app[I18N].t(lang, "p.mail.reminder_subject", **params), body)
                repo.mark_reminded(db, practice["id"], a["id"])


async def reminder_loop(app: web.Application) -> None:
    while True:
        try:
            if quiet_hours_over(now_local().hour):
                send_reminders(app)
        except Exception:  # pragma: no cover
            log.exception("Erinnerungen fehlgeschlagen")
        await asyncio.sleep(600)


REMINDER_TASK = web.AppKey("nfizyo_agenda_reminders", asyncio.Task)


async def start_reminders(app: web.Application) -> None:
    app[REMINDER_TASK] = asyncio.create_task(reminder_loop(app))


async def stop_reminders(app: web.Application) -> None:
    task = app.get(REMINDER_TASK)
    if task:
        task.cancel()


def setup(ctx: ModuleContext) -> None:
    ctx.slot("therapie.patient.right", 20, card_patient, "agenda/card_patient.html")
    ctx.slot("therapie.patient.fact", 30, fact_next)
    ctx.slot("therapie.patient.tab", 40, tab_appointments)
    ctx.slot("empfang.patient.card", 10, card_empfang, "agenda/card_empfang.html")
    ctx.slot("leitung.overview.tile", 10, tile_today)
    ctx.badge("waitlist_open", badge_waitlist)
    ctx.patient_page("/termine", p_appointments, ("GET", "POST"))
    ctx.nav("patient", NavItem("p.nav.appointments", "event", "/termine", order=30, short_key="p.nav.appointments_short"))
    ctx.slot("patient.home.card", 10, p_card_next, "agenda/p_card_next.html")
    ctx.on("note.approved", on_note_approved)
    ctx.on_startup(start_reminders)
    ctx.on_cleanup(stop_reminders)
