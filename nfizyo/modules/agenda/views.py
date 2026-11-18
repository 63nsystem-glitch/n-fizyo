"""Agenda in drei Bildschirmen.

Therapie (/therapie):  «Heute» (eigener Tag), eigene Agenda (Woche), Termine für eigene Patient/innen buchen,
                       Termin ansehen/Status. Während einer Vertretung auch der Tag der vertretenen Person. Eigene Abwesenheiten.
Empfang (/empfang):    «Heute» (Ankünfte aller), Agenda aller (Tag nach Standort, Woche pro Person), buchen,
                       Serien aus Verordnung, Warteliste, Termine im Patienten-Stammblatt, Abwesenheiten und Schliessungen.
Leitung (/leitung):    Agenda ansehen, Abwesenheiten und Schliessungen; Einstellungen Standorte/Räume/Geräte, Behandlungsarten,
                       Arbeitszeiten, Feiertage.

Feiertage, Schliessungen und Abwesenheiten erscheinen als beschriftetes Band in der Agenda. Wer einen Termin hineinlegt oder
hineinschiebt, bekommt eine Warnung und kann bewusst «Trotzdem speichern»; Serien überspringen solche Tage.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta

from aiohttp import web

from ...core import access
from ...core import patients as core_patients
from ...core.events import publish
from ...core.keys import DB, I18N
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix
from ...core.timeutil import TZ, WEEKDAYS, day_bounds_utc, fmt_date, from_iso, local_dt, now_local, to_utc_iso, week_start
from ...core.web import int_param, redirect_with_flash, render
from . import absences, gapfill, gapfill_views, repo

DAY_START, DAY_END = 7, 20  # sichtbarer Bereich der Agenda
PX_PER_MIN = 1.4  # 30 Minuten = 42 px: zwei Zeilen haben Platz
STATUSES = ("booked", "arrived", "done", "cancelled", "no_show")
# Erlaubte Statuswechsel – was nicht mehr passt, wird nicht angeboten (z. B. erledigt → nur «Zurück auf geplant»)
TRANSITIONS = {
    "booked": ("arrived", "done", "no_show", "cancelled"),
    "arrived": ("done", "booked"),
    "done": ("booked",),
    "no_show": ("arrived", "booked"),
    "cancelled": ("booked",),
    "blocked": ("cancelled",),
}
MOVABLE = ("booked", "arrived", "blocked")


# --------------------------------------------------------------------------- Hilfen
def _parse_day(value: str | None) -> date:
    try:
        return date.fromisoformat(value) if value else now_local().date()
    except ValueError:
        return now_local().date()


def _int(value) -> int | None:
    """Ganze Zahl aus Formular/Adresse (IDs, Minuten, Anzahl): nur Ziffern, höchstens 9 Stellen – sonst None.
    Riesige oder negative Werte (z. B. von Robotern) geben so nie einen Serverfehler."""
    v = str(value if value is not None else "").strip()
    return int(v) if v.isdigit() and len(v) <= 9 else None


MINUTES = (5, 600)   # Dauer eines Termins (Minuten)


def _t(request: web.Request):
    i18n, lang = request.app[I18N], request["lang"]
    return lambda key, **kw: i18n.t(lang, key, **kw)


def off_labels(request: web.Request, offs, *, name: bool = True) -> list[str]:
    t = _t(request)
    return list(dict.fromkeys(absences.describe(t, o, name=name, show_sick=show_sick(request)) for o in offs))


def show_sick(request: web.Request) -> bool:
    """Art «Krankheit» sieht nur die Leitung (Gesundheitsdaten der Mitarbeitenden); alle anderen sehen «Abwesend»."""
    return request["screen"].key == "leitung"


def is_therapie(request: web.Request) -> bool:
    return request["screen"].key == "therapie"


def own_ids(request: web.Request) -> list[int]:
    """Therapie: eigene Person + Personen, die gerade vertreten werden."""
    uid = request["user"]["id"]
    return [uid] + [d["from_user"] for d in access.delegations_for(request.app[DB], uid) if d["from_user"] != uid]


def therapist_choices(request: web.Request):
    rows = core_patients.therapists(request.app[DB])
    if is_therapie(request):
        allowed = set(own_ids(request))
        rows = [r for r in rows if r["id"] in allowed]
    return rows


def visible(request: web.Request, a) -> bool:
    """Im Therapie-Bildschirm nur eigene Termine (oder Termine eigener Patient/innen)."""
    if a is None:
        return False
    if not is_therapie(request):
        return True
    if a["therapist_id"] in own_ids(request):
        return True
    return bool(a["patient_id"]) and access.relation(request.app[DB], request["user"]["id"], a["patient_id"]) is not None


def load_appointment(request: web.Request):
    a = repo.get(request.app[DB], int_param(request, "aid"))
    if not visible(request, a):
        raise web.HTTPNotFound()
    return a


def patient_from_input(request: web.Request, value: str | None):
    """Patient aus Auswahlfeld: «Muster, Lea · 05.03.1992 · P00001» oder nur Nummer/ID.
    Im Therapie-Bildschirm nur eigene Patient/innen."""
    if not value:
        return None
    db = request.app[DB]
    m = re.search(r"(P\d{5,})\s*$", value.strip())
    if m:
        p = repo.patient_by_number(db, m.group(1))
    elif _int(value) is not None:
        p = core_patients.get(db, _int(value))
    else:
        p = None
    if p is not None and is_therapie(request) and access.relation(db, request["user"]["id"], p["id"]) is None:
        return None
    return p


def patient_label(p) -> str:
    bd = fmt_date(p["birthdate"]) if p["birthdate"] else ""
    return f"{p['last_name']}, {p['first_name']} · {bd} · {p['number']}"


def safe_back(request: web.Request, value, default: str) -> str:
    value = str(value or "")
    return value if value.startswith(prefix(request) + "/") and "//" not in value[1:] else default


def form_choices(request: web.Request) -> dict:
    db = request.app[DB]
    only = access.patient_ids_sql(request["user"]["id"]) if is_therapie(request) else None
    return {
        "therapists": therapist_choices(request), "locations": repo.locations(db), "rooms": repo.rooms(db),
        "devices": repo.devices(db), "types": repo.treatment_types(db), "patients": repo.patient_choices(db, only),
    }


async def booked(request: web.Request, aid: int, patient_id: int | None, therapist_id: int) -> None:
    """Nach dem Buchen: Therapeut/in kommt ins Behandlungsteam, andere Module erfahren es."""
    if patient_id:
        access.add_to_team(request.app[DB], patient_id, therapist_id, "appointment")
        await publish(request.app, "appointment.booked", appointment_id=aid, patient_id=patient_id, therapist_id=therapist_id)


def _layout(appts, sessions: dict | None = None):
    out = []
    for a in appts:
        s, e = from_iso(a["starts_at"]), from_iso(a["ends_at"])
        top = ((s.hour - DAY_START) * 60 + s.minute) * PX_PER_MIN
        height = max(22, (e - s).total_seconds() / 60 * PX_PER_MIN - 2)
        session = (sessions or {}).get(a["id"])
        out.append({"a": a, "top": round(top, 1), "height": round(height, 1), "start": s, "end": e, "session": session,
                    "beyond": repo.beyond_prescription(session)})
    return out


def _hours_block(h) -> dict:
    sh, sm = (int(x) for x in h["start_time"].split(":"))
    eh, em = (int(x) for x in h["end_time"].split(":"))
    top = ((sh - DAY_START) * 60 + sm) * PX_PER_MIN
    return {"top": max(0, top), "height": ((eh - sh) * 60 + (em - sm)) * PX_PER_MIN}


def _band(off, label: str) -> dict | None:
    """Beschriftetes Band (Feiertag, Schliessung, Abwesenheit) im sichtbaren Bereich der Spalte."""
    lo = datetime.combine(off.day, time(DAY_START), tzinfo=TZ)
    hi = datetime.combine(off.day, time(DAY_END), tzinfo=TZ)
    s, e = max(off.start, lo), min(off.end, hi)
    if e <= s:
        return None
    top = (s - lo).total_seconds() / 60 * PX_PER_MIN
    return {"top": round(top, 1), "height": round((e - s).total_seconds() / 60 * PX_PER_MIN, 1), "label": label, "kind": off.kind,
            "whole": off.whole_day}


def _bands(request: web.Request, offs) -> list[dict]:
    t = _t(request)
    sick = show_sick(request)
    out = [(b, o) for b, o in ((_band(o, absences.describe(t, o, show_sick=sick)), o) for o in offs) if b]
    for b, o in out:
        b["kind"] = absences.public_kind(o.kind, show_sick=sick)        # auch die CSS-Klasse verrät keine Krankheit
    return [b for b, _ in out]


def _default_week_person(db, therapists, user, start: date) -> int | None:
    """Woche ohne Auswahl: die angemeldete Person, wenn sie Termine hat – sonst die erste Person mit Terminen in dieser Woche."""
    ids = [t["id"] for t in therapists]
    if not ids:
        return None
    lo, _ = day_bounds_utc(start)
    _, hi = day_bounds_utc(start + timedelta(days=6))
    counts = Counter(r["therapist_id"] for r in repo.simple_between(db, lo, hi) if r["status"] not in ("cancelled", "no_show"))
    if user["id"] in ids and counts.get(user["id"]):
        return user["id"]
    for tid in ids:
        if counts.get(tid):
            return tid
    return user["id"] if user["id"] in ids else ids[0]


def _nobody_key(d: date) -> str:
    today = now_local().date()
    return "agenda.nobody" if d == today else ("agenda.nobody_past" if d < today else "agenda.nobody_future")


# --------------------------------------------------------------------------- Agenda (Tag / Woche)
async def calendar(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    user = request["user"]
    screen = request["screen"].key
    therapists = therapist_choices(request)
    ids = [t["id"] for t in therapists]
    # Therapie: Woche als Standard (eigene Spalte), Empfang/Leitung: Tag mit allen
    default_view = "woche" if screen == "therapie" else "tag"
    view = request.query.get("view") if request.query.get("view") in ("tag", "woche") else default_view
    d = _parse_day(request.query.get("d"))
    locs = repo.locations(db)
    loc_id = _int(request.query.get("loc")) or (locs[0]["id"] if locs else None)
    who = _int(request.query.get("who"))
    columns = []
    notices: list[str] = []
    empty_key, empty_args = None, {}
    if view == "tag":
        appts = repo.on_day(db, d, location_id=loc_id)
        sessions = repo.session_numbers(db, appts)
        offs = absences.periods(db, d, d, everyone=True)
        general = [o for o in offs if o.user_id is None]
        notices = off_labels(request, general)
        for th in therapists:
            mine = [a for a in appts if a["therapist_id"] == th["id"]]
            hours = repo.hours_on(db, th["id"], d.weekday(), loc_id)
            if not mine and not hours and screen != "therapie":
                continue
            own = [o for o in offs if o.user_id is None or o.user_id == th["id"]]
            columns.append({"key": th["id"], "title": th["name"], "sub": th["initials"], "therapist_id": th["id"], "day": d,
                            "appts": _layout(mine, sessions), "hours": [_hours_block(h) for h in hours], "bands": _bands(request, own),
                            "off": bool(own)})
        if not columns:
            empty_key = _nobody_key(d)
        prev_d, next_d = d - timedelta(days=1), d + timedelta(days=1)
    else:
        start = week_start(d)
        if who not in ids:
            who = (user["id"] if user["id"] in ids else (ids[0] if ids else None)) if screen == "therapie" \
                else _default_week_person(db, therapists, user, start)
        offs = absences.periods(db, start, start + timedelta(days=5), user_id=who) if who else []
        days = []
        for i in range(6):
            day = start + timedelta(days=i)
            days.append((day, repo.on_day(db, day, therapist_id=who) if who else []))
        sessions = repo.session_numbers(db, [a for _, appts in days for a in appts])
        t = _t(request)
        for day, appts in days:
            hours = repo.hours_on(db, who, day.weekday()) if who else []
            day_offs = [o for o in offs if o.day == day]
            # Kopfzeile: Feiertage/Schliessungen und ganztägige Abwesenheit der Person (das Band selbst liegt oft ausserhalb des Blicks)
            heads = [o for o in day_offs if o.user_id is None] + [o for o in day_offs if o.user_id is not None and o.whole_day]
            columns.append({"key": day.isoformat(), "title": fmt_date(day, request["lang"], weekday=True, year=False),
                            "sub": " · ".join(dict.fromkeys(absences.describe(t, o, name=False, show_sick=show_sick(request)) for o in heads)),
                            "therapist_id": who, "day": day, "appts": _layout(appts, sessions),
                            "hours": [_hours_block(h) for h in hours], "bands": _bands(request, day_offs), "off": bool(day_offs),
                            "today": day == now_local().date()})
        if who and not any(appts for _, appts in days):
            empty_key = "agenda.week_empty"
            empty_args = {"name": next((th["name"] for th in therapists if th["id"] == who), "")}
        prev_d, next_d = d - timedelta(days=7), d + timedelta(days=7)
    now = datetime.now(TZ)
    now_top = ((now.hour - DAY_START) * 60 + now.minute) * PX_PER_MIN if DAY_START <= now.hour < DAY_END else None
    return await render(request, "agenda/agenda.html", {
        "view": view, "day": d, "columns": columns, "locations": locs, "loc_id": loc_id, "therapists": therapists,
        "who": who, "view_chosen": request.query.get("view") in ("tag", "woche"), "hours": list(range(DAY_START, DAY_END)),
        "px_hour": 60 * PX_PER_MIN,
        "grid_height": (DAY_END - DAY_START) * 60 * PX_PER_MIN, "prev_d": prev_d.isoformat(), "next_d": next_d.isoformat(),
        "now_top": now_top, "is_today": d == now.date(), "notices": notices, "empty_key": empty_key, "empty_args": empty_args,
        "can_book": screen in ("therapie", "empfang"), "link_appts": screen in ("therapie", "empfang"),
        "show_waitlist": screen == "empfang", "show_location": screen != "therapie" or len(locs) > 1,
    })


# --------------------------------------------------------------------------- Neuer Termin
def _suggest_start(db, therapist_id: int | None, minutes: int) -> datetime | None:
    if not therapist_id:
        return None
    now = now_local()
    after = now.replace(second=0, microsecond=0) + timedelta(minutes=(15 - now.minute % 15) % 15)
    return repo.next_free(db, therapist_id, minutes, after)


async def appointment_new(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    values = dict(request.query)
    choice_ids = [t["id"] for t in therapist_choices(request)]
    found = None
    if values.get("patient"):
        found = patient_from_input(request, values["patient"])
        values["patient"] = patient_label(found) if found is not None else ""
    if not values.get("therapist_id"):
        if found is not None and found["therapist_id"] in choice_ids:
            values["therapist_id"] = found["therapist_id"]          # Hauptbehandler/in der Patientin / des Patienten
        elif is_therapie(request):
            values["therapist_id"] = request["user"]["id"]
    if request.method == "GET" and not values.get("date"):
        # sinnvoller Vorschlag: nächste freie Zeit der gewählten Person (Arbeitszeit, ohne Termine, Abwesenheiten, Feiertage)
        types = repo.treatment_types(db)
        tt = repo.treatment_type(db, _int(values.get("treatment_type_id"))) or (types[0] if types else None)
        tid = _int(values.get("therapist_id")) or (choice_ids[0] if choice_ids else None)
        suggestion = _suggest_start(db, tid, int(tt["minutes"]) if tt else 30)
        values["date"] = (suggestion or now_local()).date().isoformat()
        if suggestion is not None and not values.get("time"):
            values["time"] = suggestion.strftime("%H:%M")
            values["suggested"] = "1"
    conflicts: list[dict] = []
    offs: list[str] = []
    warn_hours = False
    errors: list[str] = []
    if request.method == "POST":
        form = await request.post()
        values = {k: form.get(k) for k in form.keys()}
        blocker = form.get("blocker") == "1"
        patient = None if blocker else patient_from_input(request, form.get("patient"))
        therapist_id = _int(form.get("therapist_id"))
        if therapist_id not in choice_ids:
            therapist_id = None
        tt = repo.treatment_type(db, _int(form.get("treatment_type_id")))
        minutes = _int(form.get("minutes")) or (tt["minutes"] if tt else 30)
        try:
            start = local_dt(date.fromisoformat(str(form.get("date"))), str(form.get("time")))
        except (ValueError, TypeError, OverflowError):
            start = None
        if not MINUTES[0] <= minutes <= MINUTES[1]:
            errors.append("time")
        if not blocker and patient is None:
            errors.append("patient")
        if not therapist_id:
            errors.append("therapist")
        if start is None:
            errors.append("time")
        if not errors:
            end = start + timedelta(minutes=minutes)
            room_id, device_id = _int(form.get("room_id")), _int(form.get("device_id"))
            conflicts = repo.conflicts(db, therapist_id=therapist_id, start_utc=to_utc_iso(start), end_utc=to_utc_iso(end),
                                       room_id=room_id, device_id=device_id, patient_id=patient["id"] if patient else None)
            warn_hours = repo.outside_hours(db, therapist_id, start, end)
            offs = off_labels(request, absences.overlapping(db, therapist_id, start, end))
            if (not conflicts and not warn_hours and not offs) or form.get("force") == "1":
                pr = core_patients.active_prescription(db, patient["id"]) if patient else None
                aid = repo.create(
                    db, therapist_id=therapist_id, start=start, minutes=minutes, patient_id=patient["id"] if patient else None,
                    location_id=_int(form.get("location_id")), room_id=room_id, device_id=device_id,
                    treatment_type_id=tt["id"] if tt else None, prescription_id=pr["id"] if pr else None,
                    status="blocked" if blocker else "booked", title=str(form.get("title") or "").strip() or None,
                    note=str(form.get("note") or "").strip() or None,
                )
                await booked(request, aid, patient["id"] if patient else None, therapist_id)
                raise redirect_with_flash(safe_back(request, form.get("back"), f"{S}/agenda?d={start.date().isoformat()}"),
                                          "agenda.created")
    return await render(request, "agenda/new.html", {"v": values, "errors": errors, "conflicts": conflicts, "offs": offs,
                                                     "warn_hours": warn_hours, "can_series": not is_therapie(request),
                                                     "setup_missing": setup_missing(request), **form_choices(request)})


SETUP_PATHS = {"locations": "standorte", "types": "behandlungen", "hours": "zeiten"}


def setup_missing(request: web.Request) -> list[dict]:
    """Neue Praxis: was für Termine noch fehlt (Standorte, Behandlungsarten, Arbeitszeiten) – mit Link für die Leitung."""
    from ...core.screens import roles_of
    from .api import available_minutes_per_week

    db = request.app[DB]
    missing = []
    if not repo.locations(db):
        missing.append("locations")
    if not repo.treatment_types(db):
        missing.append("types")
    if not any(available_minutes_per_week(db, t["id"]) for t in therapist_choices(request)):
        missing.append("hours")
    owner = "owner" in roles_of(request["user"])
    return [{"key": k, "href": f"/leitung/einstellungen/{SETUP_PATHS[k]}" if owner else None} for k in missing]


# --------------------------------------------------------------------------- Termin ansehen / ändern
async def appointment_detail(request: web.Request) -> web.StreamResponse:
    from ...core.slots import collect

    db = request.app[DB]
    a = load_appointment(request)
    gap = gapfill_views.card(request, a) if request["screen"].key == "empfang" else None
    screen = request["screen"].key
    actions = [c["ctx"] for c in await collect(request, f"{screen}.appointment.action", a)] \
        if screen in ("therapie", "empfang") and a["patient_id"] else []
    start, end = from_iso(a["starts_at"]), from_iso(a["ends_at"])
    move_offs: list[str] = []
    if request.query.get("konflikt"):
        try:
            new_start = local_dt(date.fromisoformat(request.query.get("date", "")), request.query.get("time", ""))
            th = _int(request.query.get("therapist_id")) or a["therapist_id"]
            move_offs = off_labels(request, absences.overlapping(db, th, new_start, new_start + (end - start)))
        except (ValueError, TypeError):
            move_offs = []
    session = repo.session_number(db, a)
    transitions = TRANSITIONS.get(a["status"], ())
    return await render(request, "agenda/appointment.html", {
        "a": a, "start": start, "end": end, "session": session, "beyond": repo.beyond_prescription(session),
        "gap": gap, "actions": actions, "next_status": [s for s in transitions if s != "cancelled"],
        "can_cancel": "cancelled" in transitions and a["status"] != "blocked", "can_move": a["status"] in MOVABLE,
        "can_sms_cancel": _cancel_sms_phone(db, a) is not None,
        "here_offs": off_labels(request, absences.overlapping(db, a["therapist_id"], start, end)) if a["status"] in MOVABLE else [],
        "move_offs": move_offs, **form_choices(request),
    })


async def appointment_status(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    a = load_appointment(request)
    form = await request.post()
    status = form.get("status")
    if status not in STATUSES:
        raise web.HTTPBadRequest()
    back = safe_back(request, form.get("back"), f"{prefix(request)}/agenda/termine/{a['id']}")
    if status not in TRANSITIONS.get(a["status"], ()):
        raise redirect_with_flash(back, "agenda.status_invalid")
    repo.set_status(db, a["id"], status, str(form.get("reason") or "").strip()[:300] or None)
    await publish(request.app, "appointment.status", appointment_id=a["id"], patient_id=a["patient_id"], status=status)
    if status == "cancelled" and form.get("notify") == "1":
        _sms_cancelled(request, a)
    raise redirect_with_flash(back, f"agenda.status_{status}")


def _cancel_sms_phone(db, a) -> str | None:
    """Absage per SMS möglich? Nur künftige Termine einer Person mit Handynummer (kein Festnetz)."""
    from ...core import sms as core_sms

    if not a["patient_id"] or from_iso(a["starts_at"]) <= now_local():
        return None
    p = core_patients.get(db, a["patient_id"])
    number = core_sms.normalize_phone(p["phone"]) if p is not None else None
    return number if number and not core_sms.is_landline(number) else None


def _sms_cancelled(request: web.Request, a) -> None:
    """Die Praxis sagt einen Termin ab → auf Wunsch SMS an die Person, in ihrer Sprache (gleicher Text wie beim Archivieren)."""
    from ...core.keys import SMS
    from ...core.tenant import current_practice_id

    db = request.app[DB]
    number = _cancel_sms_phone(db, a)
    if number is None:
        return
    p = core_patients.get(db, a["patient_id"])
    lang = p["lang"] or "de"
    practice = db.one("SELECT name, phone FROM practices WHERE id = ?", (current_practice_id(),))
    start = from_iso(a["starts_at"])
    text = request.app[I18N].t(lang, "p.sms.cancelled", practice=practice["name"], phone=practice["phone"] or "",
                               dates=f"{fmt_date(start, lang, weekday=True, year=False)} {start:%H:%M}")
    request.app[SMS].send(current_practice_id(), "sms", number, text)


async def appointment_move(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    a = load_appointment(request)
    form = await request.post()
    if a["status"] not in MOVABLE:
        raise redirect_with_flash(f"{S}/agenda/termine/{a['id']}", "agenda.not_movable")
    try:
        start = local_dt(date.fromisoformat(str(form.get("date"))), str(form.get("time")))
    except (ValueError, TypeError, OverflowError):
        raise redirect_with_flash(f"{S}/agenda/termine/{a['id']}", "agenda.invalid_time")
    minutes = int((from_iso(a["ends_at"]) - from_iso(a["starts_at"])).total_seconds() // 60)
    minutes = _int(form.get("minutes")) or minutes
    if not MINUTES[0] <= minutes <= MINUTES[1]:
        raise redirect_with_flash(f"{S}/agenda/termine/{a['id']}", "agenda.invalid_time")
    end = start + timedelta(minutes=minutes)
    therapist_id = _int(form.get("therapist_id")) or a["therapist_id"]
    if therapist_id != a["therapist_id"] and therapist_id not in [t["id"] for t in therapist_choices(request)]:
        therapist_id = a["therapist_id"]
    room_id = _int(form.get("room_id"))
    device_id = _int(form.get("device_id"))
    found = repo.conflicts(db, therapist_id=therapist_id, start_utc=to_utc_iso(start), end_utc=to_utc_iso(end),
                           room_id=room_id, device_id=device_id, patient_id=a["patient_id"], exclude_id=a["id"])
    offs = absences.overlapping(db, therapist_id, start, end)
    if (found or offs) and form.get("force") != "1":
        raise redirect_with_flash(f"{S}/agenda/termine/{a['id']}?konflikt=1&date={start.date()}&time={form.get('time')}"
                                  f"&therapist_id={therapist_id}", "agenda.conflict" if found else "agenda.in_off")
    repo.update(db, a["id"], starts_at=to_utc_iso(start), ends_at=to_utc_iso(end), therapist_id=therapist_id,
                room_id=room_id, device_id=device_id, note=str(form.get("note") or "").strip() or None)
    if therapist_id != a["therapist_id"]:
        await booked(request, a["id"], a["patient_id"], therapist_id)
    raise redirect_with_flash(f"{S}/agenda?d={start.date().isoformat()}", "agenda.moved")


# --------------------------------------------------------------------------- Serie aus Verordnung (Empfang)
def _remaining(db, patient) -> tuple[int | None, int | None]:
    """(noch offene verordnete Sitzungen, verordnet) der aktiven Verordnung – (None, None) ohne Verordnung."""
    pr = core_patients.active_prescription(db, patient["id"]) if patient else None
    if not pr:
        return None, None
    u = repo.rx_usage(db, [pr["id"]]).get(pr["id"], {"used": 0, "planned": 0})
    # verbraucht (inkl. «bisher» aus der Datenübernahme) + schon geplante Termine
    return max((pr["sessions"] or 0) - u["used"] - u["planned"], 0), pr["sessions"]


async def series(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    params = dict(request.query)
    preview: list[dict] = []
    patient = patient_from_input(request, params.get("patient"))
    form = None
    count = None
    if request.method == "POST":
        form = await request.post()
        params = {k: form.get(k) for k in form.keys()}
        patient = patient_from_input(request, form.get("patient"))
        weekdays = sorted({int(x) for x in form.getall("weekday", []) if str(x) in "0123456" and len(str(x)) == 1})
        therapist_id = _int(form.get("therapist_id"))
        if therapist_id not in [t["id"] for t in therapist_choices(request)]:
            therapist_id = None
        tt = repo.treatment_type(db, _int(form.get("treatment_type_id")))
        minutes = tt["minutes"] if tt else 30
        count = max(1, min(_int(form.get("count")) or 9, 36))
        try:
            start_day = date.fromisoformat(str(form.get("start")))
        except ValueError:
            start_day = now_local().date()
        hhmm = str(form.get("time") or "08:00")
        if patient and therapist_id and weekdays:
            room_id, device_id = _int(form.get("room_id")), _int(form.get("device_id"))

            def skip(s, e):
                return off_labels(request, absences.overlapping(db, therapist_id, s, e))

            try:
                slots, skipped = repo.series_slots(start_day, weekdays, hhmm, count, minutes, skip=skip)
            except ValueError:
                slots, skipped = [], []
            for s, e in slots:
                c = repo.conflicts(db, therapist_id=therapist_id, start_utc=to_utc_iso(s), end_utc=to_utc_iso(e),
                                   room_id=room_id, device_id=device_id, patient_id=patient["id"])
                preview.append({"start": s, "end": e, "conflicts": c, "outside": repo.outside_hours(db, therapist_id, s, e), "skipped": None})
            for s, reasons in skipped:
                preview.append({"start": s, "end": s + timedelta(minutes=minutes), "conflicts": [], "outside": False, "skipped": reasons})
            preview.sort(key=lambda x: x["start"])
            if form.get("create") == "1" and slots:
                pr = core_patients.active_prescription(db, patient["id"])
                first = None
                not_created = [x["start"] for x in preview if x["skipped"]]
                with db.tx():
                    sid = repo.create_series(db, patient["id"], pr["id"] if pr else None)
                    for item in preview:
                        if item["skipped"]:
                            continue
                        if item["conflicts"] and form.get("skip_conflicts", "1") == "1":
                            not_created.append(item["start"])
                            continue
                        aid = repo.create(db, therapist_id=therapist_id, start=item["start"], minutes=minutes, patient_id=patient["id"],
                                          location_id=_int(form.get("location_id")), room_id=room_id, device_id=device_id,
                                          treatment_type_id=tt["id"] if tt else None, prescription_id=pr["id"] if pr else None,
                                          series_id=sid)
                        first = first or aid
                if first:
                    await booked(request, first, patient["id"], therapist_id)
                target = f"{S}/patienten/{patient['id']}/termine"
                if not_created:
                    target += "?uebersprungen=" + ",".join(sorted({s.date().isoformat() for s in not_created}))
                raise redirect_with_flash(target, "agenda.series_created")
    remaining, prescribed = _remaining(db, patient)
    if patient is not None:
        params["patient"] = patient_label(patient)
    default_count = remaining if remaining else (1 if remaining == 0 else 9)
    planned = count or _int(params.get("count")) or default_count
    source = form if form is not None else request.query
    selected = [int(x) for x in source.getall("weekday", []) if str(x) in "0123456" and len(str(x)) == 1]
    return await render(request, "agenda/series.html", {"p": params, "patient": patient, "preview": preview,
                                                        "remaining": remaining, "prescribed": prescribed, "default_count": default_count,
                                                        "exceeds": remaining is not None and planned > remaining, "planned": planned,
                                                        "n_skipped": sum(1 for x in preview if x["skipped"]),
                                                        "weekdays_selected": selected,
                                                        "wd_names": WEEKDAYS.get(request["lang"], WEEKDAYS["de"]),
                                                        **form_choices(request)})


# --------------------------------------------------------------------------- Warteliste (Empfang)
async def waitlist_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/agenda/warteliste"
    if request.method == "POST":
        form = await request.post()
        if _int(form.get("done")) is not None:
            repo.waitlist_done(db, _int(form.get("done")))
            raise redirect_with_flash(back, "common.saved")
        patient = patient_from_input(request, form.get("patient"))
        name = str(form.get("name") or "").strip()[:120]
        if not patient and not name:
            raise redirect_with_flash(back, "agenda.waitlist_need_name")
        # Auswahlfelder nur mit Werten aus den Listen der Praxis (sonst leer) – nie ein Serverfehler (Fremdschlüssel)
        choices = form_choices(request)
        def pick(key, rows):
            v = _int(form.get(key))
            return v if v in {r["id"] for r in rows} else None
        repo.add_waitlist(db, patient_id=patient["id"] if patient else None, name=name or None,
                          phone=str(form.get("phone") or "").strip()[:40] or None, wish=str(form.get("wish") or "").strip()[:300] or None,
                          preferred=str(form.get("preferred") or "").strip()[:120] or None,
                          therapist_id=pick("therapist_id", choices["therapists"]), location_id=pick("location_id", choices["locations"]),
                          treatment_type_id=pick("treatment_type_id", choices["types"]))
        raise redirect_with_flash(back, "agenda.waitlist_added")
    return await render(request, "agenda/waitlist.html", {"items": repo.waitlist(db), "gaps": gapfill.open_gaps(db),
                                                          **form_choices(request)})


# --------------------------------------------------------------------------- Anmelden
def setup(ctx: ModuleContext) -> None:
    from . import absence_views, hooks, settings_views, today

    A = "/agenda/termine/{aid:\\d+}"
    # Therapie
    ctx.page("therapie", "/heute", today.therapie_today)
    ctx.page("therapie", "/agenda", calendar)
    ctx.page("therapie", "/agenda/neu", appointment_new, ("GET", "POST"))
    ctx.page("therapie", A, appointment_detail)
    ctx.page("therapie", A + "/status", appointment_status, ("POST",))
    ctx.page("therapie", A + "/verschieben", appointment_move, ("POST",))
    ctx.page("therapie", "/patienten/{pid:\\d+}/termine", hooks.patient_appointments_t)
    ctx.nav("therapie", NavItem("nav.today", "today", "/heute", order=10, phone=True, short_key="nav.today_short"))
    ctx.nav("therapie", NavItem("nav.agenda", "calendar_month", "/agenda", order=30))
    from . import ical_views

    ctx.public("/kalender/{token:[A-Za-z0-9_-]{20,100}}.ics", ical_views.feed)
    ctx.page("therapie", "/agenda/kalender-abo", ical_views.manage, ("GET", "POST"))
    # Empfang
    ctx.page("empfang", "/heute", today.empfang_today)
    ctx.page("empfang", "/agenda", calendar)
    ctx.page("empfang", "/agenda/neu", appointment_new, ("GET", "POST"))
    ctx.page("empfang", A, appointment_detail)
    ctx.page("empfang", A + "/status", appointment_status, ("POST",))
    ctx.page("empfang", A + "/verschieben", appointment_move, ("POST",))
    ctx.page("empfang", "/agenda/serie", series, ("GET", "POST"))
    ctx.page("empfang", "/agenda/warteliste", waitlist_page, ("GET", "POST"))
    ctx.page("empfang", "/patienten/{pid:\\d+}/termine", hooks.patient_appointments_r)
    ctx.nav("empfang", NavItem("nav.today", "today", "/heute", order=5, short_key="nav.today_short"))
    ctx.nav("empfang", NavItem("nav.agenda", "calendar_month", "/agenda", order=10))
    ctx.nav("empfang", NavItem("nav.waitlist", "hourglass_top", "/agenda/warteliste", order=15, badge="waitlist_open", short_key="nav.waitlist_short"))
    # Leitung
    ctx.page("leitung", "/agenda", calendar)
    ctx.nav("leitung", NavItem("nav.agenda", "calendar_month", "/agenda", order=20))
    settings_views.setup(ctx)
    absence_views.setup(ctx)
    hooks.setup(ctx)
    gapfill_views.setup(ctx)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('appointments', 'appointment_series', 'waitlist', 'slot_offer_recipients'), (
        "UPDATE appointments SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE appointment_series SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE waitlist SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE slot_offer_recipients SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
