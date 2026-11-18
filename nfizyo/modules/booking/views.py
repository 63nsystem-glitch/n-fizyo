"""Online-Termine.

Öffentlich (ohne Anmeldung, in der Sprache der Patient/innen):  /p/termin/<kurzname-der-praxis>
  Behandlungsart → freie Zeit → Angaben → SMS-Code → Anfrage. Kein Termin ohne Bestätigung durch den Empfang.
Empfang:  /empfang/online-anfragen  – bestätigen (bestehende Person wählen – mit Vorschlägen bei ähnlichem Namen, Geburtsdatum
          oder Telefon – oder neu anlegen; der Termin wird angelegt) oder ablehnen (Grund in eigenem Dialog); Nachricht per SMS/E-Mail.
Hintergrund: abgelehnte/abgelaufene Anfragen werden regelmässig (alle 6 Stunden) nach 90 Tagen gelöscht.
Leitung:  Einstellungen › Online-Termine  – ein/aus, buchbare Arten und Personen, Vorlauf, Link für die Website.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from aiohttp import web

from ...ai import translate as ai_translate
from ...core import patients as core_patients
from ...core.i18n import PATIENT_LANGS
from ...core.keys import AI, DB, I18N, JOBS, MAIL, MODULES, SETTINGS, SMS, THROTTLE
from ...core.mail import valid_email
from ...core.modules import ModuleContext, NavItem, module_enabled
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.security import new_code, sign, unsign
from ...core.sms import anon_sms_blocked, anon_sms_hit, ip_bucket, is_ch_li_mobile, normalize_phone
from ...core.tenant import current_practice_id, practice_scope
from ...core.timeutil import TZ, fmt_date, fmt_time, from_iso, today
from ...core.web import client_ip, public_url, redirect_with_flash, render
from ..agenda import api as agenda_api
from . import repo

log = logging.getLogger(__name__)

TOKEN_SLOT = "booking-slot"
TOKEN_REQ = "booking-req"
# Bremsen (core/auth.py LoginThrottle, echte Adresse über client_ip, IPv6 pro /64): Anfragen und falsche Codes pro Adresse
# 8 in 15 Minuten; SMS-Codes höchstens 3 pro Nummer und 6 pro Adresse in einer Stunde, dazu eine Obergrenze pro Praxis für alle
# SMS ohne Anmeldung (core/sms.py ANON_SMS_PER_HOUR / _PER_DAY). SMS nur an Handynummern CH/FL (+41 / +423).
REQ_LIMIT = 8
SMS_PER_PHONE = 3
SMS_PER_IP = 6
HOUR = 3600


def _id(value) -> int | None:
    """Zahl aus Formular/Adresse (IDs, Einstellungen): nur Ziffern, höchstens 9 Stellen – sonst None (nie ein Serverfehler)."""
    v = str(value or "").strip()
    return int(v) if v.isdigit() and len(v) <= 9 else None


def _practice_by_slug(request: web.Request):
    db = request.app[DB]
    practice = db.one("SELECT * FROM practices WHERE slug = ?", (request.match_info["slug"],))
    if practice is None or not module_enabled(db, practice["id"], "booking", request.app[MODULES]):
        raise web.HTTPNotFound()
    return practice


def _when(start: datetime, lang: str) -> str:
    return f"{fmt_date(start, lang, weekday=True)} {fmt_time(start)}"


def _type_name(t, lang: str) -> str:
    return (t["name_fr"] if lang == "fr" else t["name_de"]) or t["name_de"]


def _source(de: str | None, fr: str | None) -> tuple[str, str]:
    de, fr = (de or "").strip(), (fr or "").strip()
    return (de, "de") if de else (fr, "fr")


def _ptext(db, practice_id: int, de: str | None, fr: str | None, lang: str) -> dict:
    """Text der Praxis (Hinweis, Name der Behandlungsart) in der Sprache der Besucherin / des Besuchers: eigener Text der
    Praxis (DE/FR) oder – für die übrigen Sprachen – die maschinelle Übersetzung aus dem Zwischenspeicher (Original abrufbar).
    Fehlt sie noch, steht das Original da und "missing" ist gesetzt (die Übersetzung läuft im Hintergrund)."""
    if lang == "fr" and (fr or "").strip():
        return {"text": fr.strip(), "machine": False, "missing": False}
    if lang == "de" and (de or "").strip():
        return {"text": de.strip(), "machine": False, "missing": False}
    text, src = _source(de, fr)
    if not text or src == lang:
        return {"text": text, "machine": False, "missing": False}
    row = ai_translate.cached(db, practice_id, text, src, lang)
    if row is not None and row["text"]:
        return {"text": row["text"], "original": text, "src_lang": src, "machine": True, "missing": False}
    return {"text": text, "machine": False, "missing": True}


# Übersetzungen für die öffentliche Seite: höchstens EIN Versuch je Text und Sprache, dann Pause (wächst bei jedem leeren
# Ergebnis: 15 Min., 1 h, 4 h, 16 h, höchstens 1 Tag) – anonyme Besuche füllen sonst die Warteschlange (Diktat wartet!).
# Zusätzlich höchstens TR_PER_IP neue Übersetzungsaufträge pro Adresse und Stunde.
TR_BACKOFF = (15 * 60, 3600, 4 * 3600, 16 * 3600, 24 * 3600)
TR_PER_IP = 3
TRANSLATE_TRIES = web.AppKey("nfizyo_booking_translate_tries", dict)


def _missing_key(practice_id: int, lang: str, texts: list[str]) -> tuple:
    import hashlib

    return practice_id, lang, hashlib.sha256("\x1f".join(sorted(texts)).encode("utf-8")).hexdigest()


def _may_translate(request: web.Request, key: tuple) -> bool:
    import time

    tries = request.app.get(TRANSLATE_TRIES)
    if tries is None:                         # (nur ohne setup(), z. B. in Tests mit eigener App)
        return False
    n, last = tries.get(key, (0, 0.0))
    now = time.time()
    if n and now - last < TR_BACKOFF[min(n, len(TR_BACKOFF)) - 1]:
        return False
    ip_key = f"booking-tr:{ip_bucket(client_ip(request))}"
    throttle = request.app[THROTTLE]
    if throttle.blocked(ip_key, TR_PER_IP, HOUR):
        return False
    throttle.hit(ip_key, HOUR)
    if len(tries) > 5000:                      # Speicher begrenzen (alte Einträge vergessen)
        tries.clear()
    tries[key] = (n + 1, now)
    return True


def _practice_texts(request: web.Request, practice_id: int, st, types) -> tuple[dict, dict]:
    db, lang = request.app[DB], request["lang"]
    intro = _ptext(db, practice_id, st["intro_de"], st["intro_fr"], lang)
    names = {t["id"]: _ptext(db, practice_id, t["name_de"], t["name_fr"], lang) for t in types}
    missing = [x["text"] for x in [intro, *names.values()] if x["missing"]]
    if missing and lang in PATIENT_LANGS and request.app[AI].can_translate():
        jobs = request.app[JOBS]
        lang_index = PATIENT_LANGS.index(lang)
        if not jobs.pending(practice_id, "translate_booking", lang_index) \
                and _may_translate(request, _missing_key(practice_id, lang, missing)):
            jobs.enqueue(practice_id, "translate_booking", lang_index)
    return intro, names


async def job_translate(app: web.Application, practice_id: int, lang_index: int) -> None:
    """Hinweis und Behandlungsarten der Online-Termine in eine Patientensprache übersetzen (Zwischenspeicher der Praxis)."""
    lang = PATIENT_LANGS[lang_index]
    db = app[DB]
    st = repo.settings(db)
    for de, fr in [(st["intro_de"], st["intro_fr"])] + [(t["name_de"], t["name_fr"]) for t in repo.bookable_types(db)]:
        if lang == "fr" and (fr or "").strip():
            continue
        text, src = _source(de, fr)
        if text and src != lang:                  # translate_cached ruft das Modell nur für noch fehlende Texte auf
            await asyncio.to_thread(app[AI].translate, db, practice_id, text, src, lang)


# --------------------------------------------------------------------------- Öffentlich
async def public_page(request: web.Request) -> web.StreamResponse:
    practice = _practice_by_slug(request)
    db = request.app[DB]
    lang = request["lang"]
    with practice_scope(practice["id"]):
        st = repo.settings(db)
        types = repo.bookable_types(db)
        intro, names = _practice_texts(request, practice["id"], st, types)
        base = {"practice": practice, "st": st, "intro": intro, "names": names,
                "machine": intro["machine"] or any(n["machine"] for n in names.values()),
                "path": request.path, "step": "types", "types": types}
        if not st["enabled"] or not types or not repo.bookable_therapists(db):
            return await render(request, "booking/public.html", {**base, "step": "closed"})
        if request.method == "POST":
            return await _public_post(request, practice, base)
        types_by_id = {t["id"]: t for t in types}
        type_id = _id(request.query.get("art"))
        if type_id is None and len(types) == 1:
            type_id = types[0]["id"]
        if type_id not in types_by_id:
            return await render(request, "booking/public.html", base)
        type_row = types_by_id[type_id]
        token = request.query.get("s")
        if token:
            slot = unsign(request.app[SETTINGS].secret_key, TOKEN_SLOT, token)
            if not slot or slot.get("ty") != type_id:
                return await render(request, "booking/public.html", {**base, "step": "slots", "type": type_row, "error": "p.booking.err_slot",
                                                                        **_slot_ctx(request, db, st, type_row)})
            start = from_iso(slot["s"])
            return await render(request, "booking/public.html", {
                **base, "step": "data", "type": type_row, "slot_token": token, "when": _when(start, lang),
                "therapist": _therapist_name(db, slot["t"]) if st["show_therapists"] else None,
                "location": _location_name(db, slot.get("l")), "values": {}, "errors": [], "insurance": repo.INSURANCE})
        return await render(request, "booking/public.html", {**base, "step": "slots", "type": type_row, **_slot_ctx(request, db, st, type_row)})


def _therapist_name(db, uid: int) -> str | None:
    u = next((u for u in repo.bookable_therapists(db) if u["id"] == uid), None)
    return u["name"] if u else None


def _location_name(db, lid: int | None) -> str | None:
    return next((loc["name"] for loc in repo.locations(db) if loc["id"] == lid), None) if lid else None


def _slot_ctx(request: web.Request, db, st, type_row) -> dict:
    today = datetime.now(TZ).date()
    try:
        start_day = max(date.fromisoformat(request.query.get("ab", "")), today)
    except ValueError:
        start_day = today
    last_day = today + timedelta(days=int(st["days_ahead"]))
    start_day = min(start_day, last_day)
    loc_filter = _id(request.query.get("ort"))
    slots = repo.free_slots(db, type_row, start_day=start_day, days=7)
    slot_locs = sorted({s.location_id for s in slots if s.location_id})
    secret = request.app[SETTINGS].secret_key
    show = bool(st["show_therapists"])
    names = {u["id"]: u["name"] for u in repo.bookable_therapists(db)} if show else {}
    locs = {loc["id"]: loc["name"] for loc in repo.locations(db)}
    days: dict[str, list] = {}
    seen: set = set()
    for s in slots:
        if loc_filter and s.location_id != loc_filter:
            continue
        key = (s.start, s.therapist_id if show else None, s.location_id)
        if key in seen:
            continue
        seen.add(key)
        token = sign(secret, TOKEN_SLOT, {"ty": type_row["id"], "t": s.therapist_id, "s": s.start.isoformat(), "l": s.location_id}, 3600)
        days.setdefault(s.start.date().isoformat(), []).append(
            {"time": s.start.strftime("%H:%M"), "token": token, "who": names.get(s.therapist_id), "where": locs.get(s.location_id)})
    multi_loc = len(slot_locs) > 1 and not loc_filter
    closures = [c for c in agenda_api.closures_ahead(db, start_day, 13) if c["from"] <= last_day]
    return {"days": days, "start_day": start_day.isoformat(), "multi_loc": multi_loc, "ort": loc_filter, "closures": closures,
            "loc_choices": [{"id": lid, "name": locs.get(lid)} for lid in slot_locs] if len(slot_locs) > 1 else [],
            "prev_day": (start_day - timedelta(days=7)).isoformat() if start_day > today else None,
            "next_day": (start_day + timedelta(days=7)).isoformat() if start_day + timedelta(days=7) <= last_day else None}


def _clean(form) -> tuple[dict, list[str]]:
    errors = []
    data = {
        "first_name": " ".join(str(form.get("first_name") or "").split())[:60],
        "last_name": " ".join(str(form.get("last_name") or "").split())[:60],
        "birthdate": str(form.get("birthdate") or "")[:10],
        "phone": normalize_phone(str(form.get("phone") or "")[:30]),
        "email": str(form.get("email") or "").strip()[:120] or None,
        "email_ok": form.get("email_ok") == "1",
        "insurance": form.get("insurance") if form.get("insurance") in repo.INSURANCE else None,
        "has_prescription": form.get("has_prescription") == "1",
        "note": str(form.get("note") or "").strip()[:300] or None,
        "phone_raw": str(form.get("phone") or "")[:30],
    }
    if not data["first_name"]:
        errors.append("first_name")
    if not data["last_name"]:
        errors.append("last_name")
    try:
        bd = date.fromisoformat(data["birthdate"])
        if not (date(1900, 1, 1) <= bd <= today()):
            raise ValueError
    except ValueError:
        errors.append("birthdate")
    if not is_ch_li_mobile(data["phone"]):   # nur Schweizer/Liechtensteiner Handynummern (keine SMS ins Ausland)
        errors.append("phone")
    if data["email"] and not valid_email(data["email"]):
        errors.append("email")
    if form.get("consent") != "1":
        errors.append("consent")
    return data, errors


async def _public_post(request: web.Request, practice, base: dict) -> web.StreamResponse:
    db = request.app[DB]
    settings = request.app[SETTINGS]
    lang = request["lang"]
    form = await request.post()
    throttle = request.app[THROTTLE]
    ip = ip_bucket(client_ip(request))          # IPv6: ganzes /64 zählt als eine Adresse
    key = f"booking:{ip}"
    types_by_id = {t["id"]: t for t in base["types"]}
    if form.get("step") == "data":
        slot = unsign(settings.secret_key, TOKEN_SLOT, str(form.get("slot") or ""))
        type_row = types_by_id.get(slot.get("ty")) if slot else None
        if not slot or type_row is None:
            return await render(request, "booking/public.html", {**base, "error": "p.booking.err_slot"}, status=400)
        start = from_iso(slot["s"])
        data, errors = _clean(form)
        ctx = {**base, "step": "data", "type": type_row, "slot_token": str(form.get("slot")), "when": _when(start, lang),
               "therapist": _therapist_name(db, slot["t"]) if base["st"]["show_therapists"] else None,
               "location": _location_name(db, slot.get("l")), "values": data, "errors": errors, "insurance": repo.INSURANCE}
        if str(form.get("website") or ""):            # Falle für Roboter: unsichtbares Feld
            return await render(request, "booking/public.html", {**base, "step": "sent"})
        if throttle.blocked(key, REQ_LIMIT):
            return await render(request, "booking/public.html", {**ctx, "error": "p.booking.err_blocked"}, status=429)
        if errors:
            return await render(request, "booking/public.html", {**ctx, "error": "p.booking.err_phone" if "phone" in errors else None},
                                status=400)
        phone_key, ip_sms = f"booking-sms:{data['phone']}", f"booking-sms-ip:{ip}"
        if throttle.blocked(phone_key, SMS_PER_PHONE, HOUR) or throttle.blocked(ip_sms, SMS_PER_IP, HOUR):
            return await render(request, "booking/public.html", {**ctx, "error": "p.booking.err_sms_limit"}, status=429)
        if anon_sms_blocked(throttle, practice["id"]):     # Obergrenze der Praxis (alle Adressen zusammen)
            return await render(request, "booking/public.html", {**ctx, "error": "p.booking.err_busy"}, status=429)
        if not repo.slot_still_free(db, type_row, slot["t"], start, slot.get("l")):
            return await render(request, "booking/public.html", {**base, "step": "slots", "type": type_row, "error": "p.booking.err_taken",
                                                                    **_slot_ctx(request, db, base["st"], type_row)}, status=409)
        throttle.fail(key)    # zählt Anfragen pro Adresse (max. 8 in 15 Minuten)
        rid = repo.create_request(db, type_row=type_row, therapist_id=slot["t"], start=start, location_id=slot.get("l"),
                                  data={**data, "lang": lang})
        code = new_code(6)
        repo.set_code(db, rid, repo.code_hash(settings.secret_key, rid, code))
        request.app[SMS].send(practice["id"], "sms", data["phone"],
                              request.app[I18N].t(lang, "p.sms.booking_code", code=code, practice=practice["name"]), secrets=(code,))
        throttle.hit(phone_key, HOUR)
        throttle.hit(ip_sms, HOUR)
        anon_sms_hit(throttle, practice["id"])
        return await render(request, "booking/public.html", {
            **base, "step": "code", "rid": sign(settings.secret_key, TOKEN_REQ, {"r": rid}, 1200),
            "demo_code": code if settings.demo else None, "when": _when(start, lang)})
    if form.get("step") == "code":
        data = unsign(settings.secret_key, TOKEN_REQ, str(form.get("rid") or ""))
        if not data:
            return await render(request, "booking/public.html", {**base, "error": "p.booking.err_expired"}, status=400)
        code = "".join(ch for ch in str(form.get("code") or "") if ch.isdigit())
        result = repo.verify(db, settings.secret_key, int(data["r"]), code)
        if result == "ok":
            r = repo.get(db, int(data["r"]))
            return await render(request, "booking/public.html", {**base, "step": "sent",
                                                                  "when": _when(from_iso(r["starts_at"]), lang)})
        if result == "wrong":
            throttle.fail(key)
            return await render(request, "booking/public.html", {**base, "step": "code", "rid": str(form.get("rid")),
                                                                  "error": "p.booking.err_code"}, status=400)
        return await render(request, "booking/public.html", {**base, "error": "p.booking.err_expired"}, status=400)
    raise web.HTTPBadRequest()


# --------------------------------------------------------------------------- Empfang
def _notify(request: web.Request, practice, r, key: str, **params) -> None:
    """Nachricht an die anfragende Person in ihrer Sprache: SMS immer, E-Mail nur mit Einverständnis."""
    i18n = request.app[I18N]
    lang = r["lang"]
    params = {"practice": practice["name"], "phone": practice["phone"] or "", **params}
    request.app[SMS].send(practice["id"], "sms", r["phone"], i18n.t(lang, f"p.sms.{key}", **params))
    if r["email_ok"] and r["email"]:
        body = i18n.t(lang, f"p.mail.{key}_body", name=r["first_name"], **params) + "\n\n" + i18n.t(lang, "p.mail.footer", **params)
        request.app[MAIL].send(practice["id"], r["email"], i18n.t(lang, f"p.mail.{key}_subject", **params), body)


async def requests_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    practice = db.one("SELECT * FROM practices WHERE id = ?", (current_practice_id(),))
    if request.method == "POST":
        form = await request.post()
        rid = _id(form.get("request_id"))
        r = repo.get(db, rid) if rid else None
        if r is None:
            raise web.HTTPNotFound()
        place = repo.locations(db)
        where = next((loc["name"] for loc in place if loc["id"] == r["location_id"]), practice["name"])
        when = _when(from_iso(r["starts_at"]), r["lang"])
        if form.get("do") == "confirm":
            if r["status"] == "requested" and agenda_api.unavailable(db, r["therapist_id"], from_iso(r["starts_at"]), from_iso(r["ends_at"])):
                raise redirect_with_flash(f"{S}/online-anfragen", "booking.unavailable")
            choice = str(form.get("patient_choice") or "")
            chosen = None
            if choice == "other":
                chosen = repo.patient_for_choice(db, form.get("patient"))
                if chosen is None:
                    raise redirect_with_flash(f"{S}/online-anfragen", "booking.pick_patient")
            elif choice.isdigit():
                chosen = repo.patient_for_choice(db, choice)
                if chosen is None:
                    raise redirect_with_flash(f"{S}/online-anfragen", "booking.invalid")
            elif choice not in ("", "new"):
                raise redirect_with_flash(f"{S}/online-anfragen", "booking.invalid")
            if r["status"] != "requested":
                raise redirect_with_flash(f"{S}/online-anfragen", "booking.already_decided")
            patient_id, aid = repo.confirm(db, r, patient_id=chosen["id"] if chosen else None, create_new=choice == "new")
            if aid is None:
                # nichts wurde angelegt (auch keine neue Person) – die Anfrage bleibt offen
                raise redirect_with_flash(f"{S}/online-anfragen", "booking.taken")
            _notify(request, practice, r, "booking_confirmed", when=when, place=where)
            other = [a for a in agenda_api.same_day(db, patient_id, from_iso(r["starts_at"])) if a["id"] != aid]
            raise redirect_with_flash(f"{S}/patienten/{patient_id}", "booking.confirmed_same_day" if other else "booking.confirmed")
        if form.get("do") == "decline":
            if repo.decline(db, r, str(form.get("reason") or "").strip()):
                _notify(request, practice, r, "booking_declined", when=when)
            raise redirect_with_flash(f"{S}/online-anfragen", "booking.declined")
        raise redirect_with_flash(f"{S}/online-anfragen", "booking.invalid")
    types = {t["id"]: t for t in agenda_api.lookup_tables(db)["types"]}
    items = []
    for r in repo.open_requests(db):
        start, end = from_iso(r["starts_at"]), from_iso(r["ends_at"])
        likely = repo.likely_patients(db, r)
        for m in likely:      # wie beim Lücken füllen: «am selben Tag schon ein Termin» (Uhrzeit)
            m["same_day"] = [fmt_time(from_iso(a["starts_at"])) for a in repo.same_day(db, r, m["p"]["id"])]
        items.append({"r": r, "type": types.get(r["treatment_type_id"]), "match": repo.existing_patient(db, r),
                      "likely": likely, "start": start,
                      "unavailable": agenda_api.unavailable(db, r["therapist_id"], start, end)})
    st = repo.settings(db)
    patients = core_patients.search(db, "", limit=2000) if items else []
    return await render(request, "booking/requests.html", {"items": items, "recent": repo.recent_decided(db), "enabled": st["enabled"],
                                                          "types": types, "patients": patients})


def badge_open(db, practice_id: int, user) -> int:
    with practice_scope(practice_id):
        return repo.count_open(db)


PURGE_INTERVAL = 6 * 3600          # Sekunden: abgelehnte/abgelaufene Anfragen regelmässig löschen (nicht nur beim Start)


def purge_all(app: web.Application) -> None:
    db = app[DB]
    for practice in db.query("SELECT id FROM practices"):
        with practice_scope(practice["id"]):
            repo.purge_old(db)


async def purge_loop(app: web.Application) -> None:
    while True:
        try:
            purge_all(app)
        except Exception:  # pragma: no cover - die Schleife darf nie sterben
            log.exception("Online-Anfragen: Löschen alter Anfragen fehlgeschlagen")
        await asyncio.sleep(PURGE_INTERVAL)


PURGE_TASK = web.AppKey("nfizyo_booking_purge", asyncio.Task)


async def start_purge(app: web.Application) -> None:
    app[PURGE_TASK] = asyncio.create_task(purge_loop(app))


async def stop_purge(app: web.Application) -> None:
    task = app.get(PURGE_TASK)
    if task:
        task.cancel()


# --------------------------------------------------------------------------- Leitung: Einstellungen
async def settings_view(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/online-termine"
    if request.method == "POST":
        form = await request.post()

        def _int(name, lo, hi, default):
            v = _id(form.get(name))
            return v if v is not None and lo <= v <= hi else default

        step = _int("slot_step", 5, 60, 15)
        values = {"enabled": 1 if form.get("enabled") == "1" else 0, "days_ahead": _int("days_ahead", 1, 90, 21),
                  "lead_hours": _int("lead_hours", 0, 168, 12), "slot_step": step if step in (5, 10, 15, 20, 30, 60) else 15,
                  "show_therapists": 1 if form.get("show_therapists") == "1" else 0,
                  "intro_de": str(form.get("intro_de") or "").strip()[:500] or None, "intro_fr": str(form.get("intro_fr") or "").strip()[:500] or None}
        repo.save_settings(db, values, [v for v in (_id(x) for x in form.getall("types", [])) if v],
                           [v for v in (_id(x) for x in form.getall("therapists", [])) if v])
        raise redirect_with_flash(back, "common.saved")
    practice = db.one("SELECT slug FROM practices WHERE id = ?", (current_practice_id(),))
    base_url = request.app[SETTINGS].base_url
    link = public_url(request, f"/p/termin/{practice['slug']}")
    return await settings_page(request, "online-termine", "booking/s_booking.html", {
        "st": repo.settings(db), "types": agenda_api.lookup_tables(db)["types"], "type_ids": repo.type_ids(db),
        "therapists": core_patients.therapists(db), "therapist_ids": repo.therapist_ids(db),
        "link": link, "public_ok": base_url.startswith("https://")})


def setup(ctx: ModuleContext) -> None:
    ctx.public("/p/termin/{slug:[a-z0-9-]+}", public_page, ("GET", "POST"))
    ctx.job("translate_booking", job_translate, background=True)   # Praxistexte in 11 Sprachen – niemand wartet darauf
    ctx.app[TRANSLATE_TRIES] = {}             # Übersetzungsversuche der öffentlichen Seite (im Speicher, siehe _may_translate)
    ctx.page("empfang", "/online-anfragen", requests_page, ("GET", "POST"))
    ctx.nav("empfang", NavItem("nav.booking", "event_available", "/online-anfragen", order=25, badge="booking_open"))
    ctx.badge("booking_open", badge_open)
    ctx.page("leitung", "/einstellungen/online-termine", settings_view, ("GET", "POST"))
    ctx.on_startup(start_purge)
    ctx.on_cleanup(stop_purge)
    ctx.slot("leitung.settings", 45, lambda request: {"key": "online-termine", "label_key": "booking.settings_title", "path": "online-termine"})
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('booking_requests',), (
        "UPDATE booking_requests SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
