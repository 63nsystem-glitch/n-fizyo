"""Video-Sitzung – Therapie/Empfang richten den Raum ein und schicken den Link per SMS; die Therapeutin / der Therapeut startet.
Patient/in tritt über den SMS-Link (ohne Anmeldung) oder über die App bei – nach Einwilligung und nur zur Terminzeit."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp import web

from ...core import patients as core_patients
from ...core.i18n import PATIENT_LANGS
from ...core.keys import DB, I18N, MODULES, SETTINGS, SMS, THROTTLE
from ...core.modules import ModuleContext, module_enabled
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.timeutil import TZ, fmt_date, fmt_time, from_iso
from ...core.web import LANG_COOKIE, client_ip, int_param, public_url, redirect_with_flash, render
from ..agenda import api as agenda_api
from ..patients import api as patients_api
from . import jitsi, repo

PATIENT_BEFORE = timedelta(minutes=15)      # Patient/in kann 15 Minuten vorher in den Warteraum
THERAPIST_BEFORE = timedelta(minutes=30)
AFTER_END = timedelta(minutes=30)


def configured(app: web.Application) -> bool:
    s = app[SETTINGS]
    return bool(s.video_url and s.video_secret)


def _window(a, before: timedelta) -> tuple[datetime, datetime]:
    return from_iso(a["starts_at"]) - before, from_iso(a["ends_at"]) + AFTER_END


def _state(a, before: timedelta) -> str:
    if a["status"] in ("cancelled", "no_show", "blocked"):
        return "cancelled"
    now = datetime.now(timezone.utc)
    start, end = _window(a, before)
    return "early" if now < start else ("over" if now > end else "open")


def _when(a, lang: str) -> str:
    s = from_iso(a["starts_at"]).astimezone(TZ)
    return f"{fmt_date(s, lang, weekday=True, year=False)} {fmt_time(s)}"


def _jitsi_url(request: web.Request, a, room, name: str, moderator: bool, lang: str) -> str:
    s = request.app[SETTINGS]
    start, end = _window(a, THERAPIST_BEFORE if moderator else PATIENT_BEFORE)
    jwt = jitsi.token(s.video_secret, s.video_app_id, s.video_url, room["room"], name, moderator=moderator,
                      nbf=int(start.timestamp()), exp=int(end.timestamp()))
    return jitsi.room_url(s.video_url, room["room"], jwt, name, lang)


def _short_name(p) -> str:
    return f"{p['first_name']} {(p['last_name'] or '')[:1]}.".strip()


# --------------------------------------------------------------------------- Therapie / Empfang
def _appointment(request: web.Request):
    a = agenda_api.get(request.app[DB], int_param(request, "aid"))
    if a is None or not a["patient_id"]:
        raise web.HTTPNotFound()
    if request["screen"].key == "therapie" and a["therapist_id"] != request["user"]["id"] \
            and not patients_api.can_open(request, a["patient_id"]):
        raise web.HTTPNotFound()
    return a


async def page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    a = _appointment(request)
    room = repo.room_for(db, a["id"])
    back = f"{prefix(request)}/video/{a['id']}"
    if request.method == "POST":
        form = await request.post()
        if form.get("action") == "setup":
            if room is None:
                repo.create_room(db, a["id"], a["patient_id"])
            raise redirect_with_flash(back, "video.ready")
        if form.get("action") == "sms" and room is not None:
            p = core_patients.get(db, a["patient_id"])
            if not p["phone"]:
                raise redirect_with_flash(back, "video.no_phone")
            tok = repo.new_link(db, room["id"], p["id"])
            practice = db.one("SELECT name FROM practices WHERE id = ?", (current_practice_id(),))
            lang = p["lang"] or "de"
            text = request.app[I18N].t(lang, "p.sms.video", practice=practice["name"], when=_when(a, lang),
                                       url=public_url(request, f"/p/video/{tok}"))
            request.app[SMS].send(current_practice_id(), "sms", p["phone"], text, secrets=(tok,))
            raise redirect_with_flash(back, "video.sms_sent")
        raise web.HTTPBadRequest()
    p = core_patients.get(db, a["patient_id"])
    if not configured(request.app):
        # Ohne Video-Server: nur ein schlichter Satz – keine Technik, kein Einrichten
        return await render(request, "video/page.html", {"a": a, "room": None, "configured": False, "start": from_iso(a["starts_at"])})
    return await render(request, "video/page.html", {
        "a": a, "room": room, "configured": configured(request.app), "state": _state(a, THERAPIST_BEFORE), "start": from_iso(a["starts_at"]),
        "consent": repo.has_consent(db, a["patient_id"]), "phone": p["phone"], "can_start": request["screen"].key == "therapie",
    })


async def start(request: web.Request) -> web.StreamResponse:
    """Therapeut/in betritt den Raum (Moderator). Nur zur Terminzeit."""
    db = request.app[DB]
    a = _appointment(request)
    room = repo.room_for(db, a["id"])
    back = f"{prefix(request)}/video/{a['id']}"
    if room is None or not configured(request.app):
        raise redirect_with_flash(back, "video.not_configured")
    if _state(a, THERAPIST_BEFORE) != "open":
        raise redirect_with_flash(back, "video.not_now")
    repo.mark_joined(db, room["id"], "therapist")
    raise web.HTTPFound(_jitsi_url(request, a, room, request["user"]["name"], True, request["lang"]))


def action(request: web.Request, a) -> dict | None:
    """Knopf «Video-Sitzung» bei Terminen mit Patient/in – nur mit eingerichtetem Video-Server (sonst nirgends ein Video-Knopf)."""
    if a["status"] not in ("booked", "arrived"):
        return None
    if not configured(request.app):
        return None
    room = repo.room_for(request.app[DB], a["id"])
    return {"label_key": "video.action_open" if room else "video.action", "icon": "videocam",
            "href": f"{prefix(request)}/video/{a['id']}", "primary": bool(room) and request["screen"].key == "therapie"}


def today_next(request: web.Request, a) -> dict | None:
    if not configured(request.app):
        return None
    room = repo.room_for(request.app[DB], a["id"])
    if room is None:
        return None
    return {"aid": a["id"], "open": _state(a, THERAPIST_BEFORE) == "open", "configured": configured(request.app)}


# --------------------------------------------------------------------------- Patient/in
def _lang(request: web.Request, p) -> str:
    chosen = request.query.get("lang") or request.cookies.get(LANG_COOKIE)
    return chosen if chosen in PATIENT_LANGS else (p["lang"] if p["lang"] in PATIENT_LANGS else request["lang"])


async def _join_flow(request: web.Request, room, a, p, via: str, path: str) -> web.StreamResponse:
    db = request.app[DB]
    state = _state(a, PATIENT_BEFORE)
    if request.method == "POST":
        form = await request.post()
        if form.get("consent") == "1":
            repo.give_consent(db, p["id"], via)
        if not repo.has_consent(db, p["id"]):
            raise redirect_with_flash(path, "p.video.need_consent")
        if state != "open" or not configured(request.app):
            raise web.HTTPFound(path)
        repo.mark_joined(db, room["id"], "patient")
        raise web.HTTPFound(_jitsi_url(request, a, room, _short_name(p), False, request["lang"]))
    therapist = a["therapist_name"]
    return await render(request, "video/p_join.html", {
        "a": a, "start": from_iso(a["starts_at"]).astimezone(TZ), "state": state, "therapist": therapist,
        "consent": repo.has_consent(db, p["id"]), "configured": configured(request.app), "path": path,
        "opens": (from_iso(a["starts_at"]) - PATIENT_BEFORE).astimezone(TZ),
        "practice": db.one("SELECT name, phone FROM practices WHERE id = ?", (room["practice_id"],)),
    })


async def public_join(request: web.Request) -> web.StreamResponse:
    """Link aus der SMS (ohne Anmeldung)."""
    db = request.app[DB]
    throttle = request.app[THROTTLE]
    key = f"video:{client_ip(request)}"
    if throttle.blocked(key):
        return await render(request, "video/p_join.html", {"state": "invalid"}, status=429)
    room = repo.by_link(db, request.match_info["token"])
    if room is not None and not module_enabled(db, room["practice_id"], "video", request.app[MODULES]):
        room = None
    if room is None:
        throttle.fail(key)
        return await render(request, "video/p_join.html", {"state": "invalid"}, status=404)
    with practice_scope(room["practice_id"]):
        a = agenda_api.get(db, room["appointment_id"])
        p = core_patients.get(db, room["patient_id"])
        request["lang"] = _lang(request, p)
        return await _join_flow(request, room, a, p, "link", request.path)


async def app_join(request: web.Request) -> web.StreamResponse:
    """Aus der Patienten-App (angemeldet)."""
    db = request.app[DB]
    p = request["patient"]
    a = agenda_api.get(db, int_param(request, "aid"))
    room = repo.room_for(db, a["id"]) if a else None
    if a is None or room is None or a["patient_id"] != p["id"]:
        raise web.HTTPNotFound()
    return await _join_flow(request, room, a, p, "app", request.path)


def p_card(request: web.Request) -> dict | None:
    """Startseite der App: Videotermin heute oder morgen."""
    if not configured(request.app):
        return None
    db = request.app[DB]
    now = datetime.now(timezone.utc)
    tomorrow = now.astimezone(TZ).date() + timedelta(days=1)
    for room in repo.rooms_for_patient(db, request["patient"]["id"]):
        a = agenda_api.get(db, room["appointment_id"])
        if a is None or a["status"] not in ("booked", "arrived"):
            continue
        start, end = from_iso(a["starts_at"]), from_iso(a["ends_at"]) + AFTER_END
        if now <= end and start.astimezone(TZ).date() <= tomorrow:
            return {"a": a, "start": start.astimezone(TZ), "open": _state(a, PATIENT_BEFORE) == "open"}
    return None


# --------------------------------------------------------------------------- Leitung: Stand und Prüfung des Video-Servers
async def check_server(app: web.Application) -> list[tuple[str, bool]]:
    """Prüft den eigenen Jitsi-Server von hier aus: erreichbar, Datenschutz-Einstellungen von N-Fizyo aktiv, keine Gäste."""
    import aiohttp

    s = app[SETTINGS]
    out: list[tuple[str, bool]] = [("configured", configured(app)), ("https", s.video_url.startswith("https://")),
                                   ("secret_strong", len(s.video_secret) >= 32)]
    body = ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(f"{s.video_url}/config.js", allow_redirects=False) as r:
                ok = r.status == 200
                body = await r.text() if ok else ""
    except Exception:  # noqa: BLE001 – jede Störung heisst «nicht erreichbar»
        ok = False
    out.append(("reachable", ok))
    out.append(("privacy_config", "disableThirdPartyRequests = true" in body and "enabled: false" in body))
    out.append(("no_guests", bool(body) and "anonymousdomain" not in body))
    return out


async def settings_section(request: web.Request) -> web.StreamResponse:
    from ...core.pages import settings_page

    s = request.app[SETTINGS]
    checks = None
    if request.method == "POST":
        await request.post()
        checks = await check_server(request.app) if s.video_url else [("configured", False)]
    return await settings_page(request, "video", "video/s_video.html", {
        "url": s.video_url, "has_secret": bool(s.video_secret), "configured": configured(request.app),
        "checks": checks,
    })


def setup(ctx: ModuleContext) -> None:
    ctx.page("leitung", "/einstellungen/video", settings_section, ("GET", "POST"))
    ctx.slot("leitung.settings", 66, lambda request: {"key": "video", "label_key": "video.title", "path": "video"})
    for screen in ("therapie", "empfang"):
        ctx.page(screen, "/video/{aid:\\d+}", page, ("GET", "POST"))
        ctx.slot(f"{screen}.appointment.action", 20, action)
    ctx.page("therapie", "/video/{aid:\\d+}/start", start)
    ctx.slot("therapie.today.next", 20, today_next, "video/_next.html")
    ctx.public("/p/video/{token:[A-Za-z0-9_-]{20,100}}", public_join, ("GET", "POST"))
    ctx.patient_page("/video/termin/{aid:\\d+}", app_join, ("GET", "POST"))
    ctx.slot("patient.home.card", 5, p_card, "video/p_card.html")
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('video_rooms', 'video_consents'), (
        "UPDATE video_rooms SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "DELETE FROM video_consents WHERE practice_id = :pid AND patient_id = :drop AND EXISTS (SELECT 1 FROM "
        "video_consents k WHERE k.practice_id = :pid AND k.patient_id = :keep)",
        "UPDATE video_consents SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
