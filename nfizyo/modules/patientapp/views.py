"""Patienten-App (PWA): Einladung, Anmeldung, Startseite, Termine, Profil. Läuft auf dem Server der Praxis."""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from ...core.audit import audit
from ...core.auth import create_session, destroy_session, end_patient_sessions
from ...core.db import utcnow
from ...core.i18n import PATIENT_LANGS
from ...core.keys import DB, I18N, SETTINGS, SMS, THROTTLE
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix
from ...core.security import new_code, new_token, sign, unsign
from ...core.slots import collect
from ...core.sms import anon_sms_blocked, anon_sms_hit, ip_bucket, is_landline
from ...core.tenant import current_practice_id, practice_scope
from ...core.web import PATIENT_COOKIE, client_ip, link_is_public, patient_required, public_url, redirect_with_flash, render
from ..patients import api as patients_api

INVITE_DAYS = 14
CONSENT_VERSION = "2026-10"
MAX_ATTEMPTS = 5
# Anmeldung per SMS-Code – Bremsen (core/auth.py LoginThrottle, echte Adresse über client_ip):
IP_FAIL_LIMIT = 8            # Adresse: 8 Fehlversuche (unbekannte Nummer/Geburtsdatum, falscher Code) in 15 Minuten
CODES_PER_HOUR = 3           # SMS-Codes pro Telefonnummer und pro Patient/in in einer Stunde (danach freundlicher Hinweis)
IP_SMS_PER_HOUR = 10         # SMS-Codes von einer Adresse in einer Stunde
PATIENT_CODE_FAILS = 10      # falsche Codes pro Patient/in in einer Stunde (egal von wo)
HOUR = 3600


def _code_hash(secret: bytes, code: str) -> str:
    return hmac.new(secret, code.encode(), hashlib.sha256).hexdigest()


def _expires(days: int = 0, minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, minutes=minutes)).replace(microsecond=0).isoformat()


def _qr_svg(url: str) -> str:
    try:
        import segno
    except ImportError:  # QR ist Komfort – ohne Bibliothek bleibt Link + Code
        return ""
    qr = segno.make(url, error="m")
    return qr.svg_inline(scale=5, dark="#191C20", light="#FFFFFF", border=2)


# --------------------------------------------------------------------------- Personal: Einladung erstellen (Therapie und Empfang)
async def invite_create(request: web.Request) -> web.StreamResponse:
    """QR-Code + Code für die Patienten-App. Die Patientin / der Patient verbindet sich damit zu Hause selbst."""
    db = request.app[DB]
    settings = request.app[SETTINGS]
    patient = patients_api.require_patient(request)
    code = new_code(6)
    nonce = new_token(12)
    iid = db.execute(
        "INSERT INTO patient_invites (practice_id, patient_id, nonce, code_hash, expires_at, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (current_practice_id(), patient["id"], nonce, _code_hash(settings.secret_key, code), _expires(days=INVITE_DAYS),
         request["user"]["id"], utcnow()),
    )
    token = sign(settings.secret_key, "invite", {"i": iid, "n": nonce}, INVITE_DAYS * 86400)
    url = public_url(request, f"/p/einladung/{token}")
    sms_sent = False
    landline = is_landline(patient["phone"])     # Festnetz: keine SMS (käme nie an) – QR-Code zeigen oder ausdrucken
    if patient["phone"] and not landline and (await request.post()).get("sms", "1") == "1":
        text = request.app[I18N].t(patient["lang"], "p.sms.invite", name=patient["first_name"], url=url, code=code)
        request.app[SMS].send(current_practice_id(), "sms", patient["phone"], text, secrets=(token, code))
        sms_sent = True
    audit(db, "create", "invite", iid, patient["id"])
    # Link nur im Praxisnetz / auf dem Demo-Rechner erreichbar: offen sagen statt einen toten Link zu zeigen
    link_note = None if link_is_public(url) else ("pstaff.link_demo" if settings.demo else "pstaff.link_local")
    return await render(request, "patientapp/invite_created.html", {
        "patient": patient, "url": url, "code": code, "qr": _qr_svg(url), "sms_sent": sms_sent, "landline": landline, "days": INVITE_DAYS,
        "link_note": link_note,
    })


async def app_logout(request: web.Request) -> web.StreamResponse:
    """Telefon verloren oder gestohlen: Die Praxis meldet die Patienten-App auf allen Geräten ab. Die Einwilligung bleibt;
    eine neue Anmeldung braucht SMS-Code und Geburtsdatum (bei verlorenem Telefon zuerst die Nummer ändern)."""
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    n = db.value("SELECT COUNT(*) FROM sessions WHERE kind = 'patient' AND patient_id = ? AND practice_id = ?",
                 (patient["id"], current_practice_id()), 0)
    end_patient_sessions(db, current_practice_id(), patient["id"])
    audit(db, "logout", "patient_app", None, patient["id"], {"sessions": n})
    raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}", "pstaff.app_logged_out")


def action_app_logout(request: web.Request, patient) -> dict | None:
    from .api import has_app
    if not has_app(request.app[DB], patient["id"]):
        return None
    return {"label_key": "pstaff.app_logout", "icon": "logout",
            "href": f"{prefix(request)}/patienten/{patient['id']}/app-abmelden", "post": True}


def action_invite(request: web.Request, patient) -> dict | None:
    return {"label_key": "pstaff.invite", "icon": "qr_code", "href": f"{prefix(request)}/patienten/{patient['id']}/app-einladung",
            "post": True, "icon_only": request["screen"].key == "therapie"}


# --------------------------------------------------------------------------- Patient: Einladung annehmen
async def invite_open(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    settings = request.app[SETTINGS]
    data = unsign(settings.secret_key, "invite", request.match_info["token"])
    invite = None
    if data:
        invite = db.one("SELECT i.*, p.lang AS patient_lang, p.first_name, pr.name AS practice_name FROM patient_invites i"
                        " JOIN patients p ON p.id = i.patient_id JOIN practices pr ON pr.id = i.practice_id"
                        " WHERE i.id = ? AND i.nonce = ?", (data.get("i"), data.get("n")))
    if invite is None or invite["used_at"] or invite["expires_at"] < utcnow() or invite["attempts"] >= MAX_ATTEMPTS:
        return await render(request, "patientapp/invite_invalid.html", {}, status=410)
    if not request.cookies.get("nf_lang"):
        request["lang"] = invite["patient_lang"] if invite["patient_lang"] in PATIENT_LANGS else request["lang"]
    error = None
    if request.method == "POST":
        form = await request.post()
        code = "".join(ch for ch in str(form.get("code", "")) if ch.isdigit())
        if form.get("consent") != "1":
            error = "p.invite.need_consent"
        elif not hmac.compare_digest(_code_hash(settings.secret_key, code), invite["code_hash"]):
            db.execute("UPDATE patient_invites SET attempts = attempts + 1 WHERE id = ?", (invite["id"],))
            error = "p.invite.wrong_code"
        else:
            with practice_scope(invite["practice_id"], None, invite["patient_id"]):
                db.execute("UPDATE patient_invites SET used_at = ? WHERE id = ?", (utcnow(), invite["id"]))
                for kind in ("app", "translation") if form.get("consent_translation") == "1" else ("app",):
                    db.execute("INSERT INTO patient_consents (practice_id, patient_id, kind, version, accepted_at) VALUES (?,?,?,?,?)",
                               (invite["practice_id"], invite["patient_id"], kind, CONSENT_VERSION, utcnow()))
                audit(db, "accept", "invite", invite["id"], invite["patient_id"])
            token, _ = create_session(db, "patient", patient_id=invite["patient_id"], practice_id=invite["practice_id"])
            resp = web.HTTPFound("/p/heute")
            resp.set_cookie(PATIENT_COOKIE, token, httponly=True, samesite="Lax", secure=settings.secure_cookies,
                            max_age=60 * 24 * 3600, path="/")
            raise resp
    return await render(request, "patientapp/invite.html", {"invite": invite, "error": error})


# --------------------------------------------------------------------------- Patient: erneut anmelden (Code per SMS)
async def login(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    settings = request.app[SETTINGS]
    if request.get("patient") is not None:
        raise web.HTTPFound("/p/heute")
    step = "phone"
    error = None
    demo_code = None
    lid = None
    if request.method == "POST":
        form = await request.post()
        throttle = request.app[THROTTLE]
        ip = ip_bucket(client_ip(request))          # IPv6: ganzes /64 zählt als eine Adresse (SEC-04)
        key = f"p:{ip}"
        if throttle.blocked(key, IP_FAIL_LIMIT):
            error = "p.login.blocked"
        elif form.get("step") == "phone":
            phone = "".join(ch for ch in str(form.get("phone", "")) if ch.isdigit())[:20]
            bd = str(form.get("birthdate", ""))[:10]
            phone_key, ip_sms = f"p-sms-phone:{phone[-9:]}", f"p-sms-ip:{ip}"
            # Höchstens 3 Codes pro Nummer und Stunde – gezählt wird jede Anfrage für die Nummer, ob es sie gibt oder nicht
            # (der Hinweis verrät also nichts über Patient/innen).
            if len(phone) >= 9 and (throttle.blocked(phone_key, CODES_PER_HOUR, HOUR) or throttle.blocked(ip_sms, IP_SMS_PER_HOUR, HOUR)):
                return await render(request, "patientapp/login.html", {"step": "phone", "error": "p.login.too_many_codes", "lid": None,
                                                                       "demo_code": None}, status=429)
            if len(phone) >= 9:
                throttle.hit(phone_key, HOUR)
            rows = db.query(
                "SELECT p.* FROM patients p WHERE p.archived = 0 AND p.birthdate = ? AND EXISTS ("
                " SELECT 1 FROM patient_consents c WHERE c.patient_id = p.id AND c.kind = 'app' AND c.revoked_at IS NULL)", (bd,))
            match = [r for r in rows if r["phone"] and "".join(ch for ch in r["phone"] if ch.isdigit())[-9:] == phone[-9:] and len(phone) >= 9]
            if len(match) == 1 and _codes_last_hour(db, match[0]) < CODES_PER_HOUR \
                    and not anon_sms_blocked(throttle, match[0]["practice_id"]):     # Obergrenze der Praxis (SMS ohne Anmeldung)
                p = match[0]
                code = new_code(6)
                lid = db.execute("INSERT INTO patient_login_codes (practice_id, patient_id, code_hash, expires_at, created_at)"
                                 " VALUES (?,?,?,?,?)", (p["practice_id"], p["id"], _code_hash(settings.secret_key, code),
                                                          _expires(minutes=15), utcnow()))
                text = request.app[I18N].t(p["lang"], "p.sms.login", code=code)
                request.app[SMS].send(p["practice_id"], "sms", p["phone"], text, secrets=(code,))
                throttle.hit(ip_sms, HOUR)
                anon_sms_hit(throttle, p["practice_id"])
                if settings.demo:
                    demo_code = code  # Demo: SMS wird nicht versendet – Code direkt anzeigen
            elif len(match) != 1:
                throttle.fail(key)
            # (schon 3 Codes in dieser Stunde für diese Person: still keine weitere SMS – Antwort wie immer)
            step = "code"  # immer gleich antworten (keine Auskunft, ob es den Patienten gibt)
            if lid is not None:
                lid = sign(settings.secret_key, "plogin", {"l": lid}, 900)
            else:
                lid = sign(settings.secret_key, "plogin", {"l": 0}, 900)
        elif form.get("step") == "code":
            data = unsign(settings.secret_key, "plogin", str(form.get("lid", "")))
            code = "".join(ch for ch in str(form.get("code", "")) if ch.isdigit())
            row = db.one("SELECT * FROM patient_login_codes WHERE id = ?", (data.get("l") if data else 0,))
            patient_key = f"p-code:{row['patient_id']}" if row is not None else None
            if patient_key and throttle.blocked(patient_key, PATIENT_CODE_FAILS, HOUR):
                return await render(request, "patientapp/login.html", {"step": "phone", "error": "p.login.blocked", "lid": None,
                                                                       "demo_code": None}, status=429)
            ok = (row is not None and not row["used_at"] and row["expires_at"] > utcnow() and row["attempts"] < MAX_ATTEMPTS
                  and hmac.compare_digest(_code_hash(settings.secret_key, code), row["code_hash"]))
            if ok:
                db.execute("UPDATE patient_login_codes SET used_at = ? WHERE id = ?", (utcnow(), row["id"]))
                throttle.reset(patient_key)
                token, _ = create_session(db, "patient", patient_id=row["patient_id"], practice_id=row["practice_id"])
                with practice_scope(row["practice_id"], None, row["patient_id"]):
                    audit(db, "login", "patient", row["patient_id"], row["patient_id"])
                resp = web.HTTPFound("/p/heute")
                resp.set_cookie(PATIENT_COOKIE, token, httponly=True, samesite="Lax", secure=settings.secure_cookies,
                                max_age=60 * 24 * 3600, path="/")
                raise resp
            if row is not None:
                db.execute("UPDATE patient_login_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
                throttle.fail(patient_key, HOUR)
            throttle.fail(key)
            step, error, lid = "code", "p.login.wrong_code", form.get("lid")
    return await render(request, "patientapp/login.html", {"step": step, "error": error, "lid": lid, "demo_code": demo_code})


def _codes_last_hour(db, patient) -> int:
    """SMS-Codes für diese Person in der letzten Stunde (aus der Datenbank – gilt auch nach einem Neustart)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    return db.value("SELECT COUNT(*) FROM patient_login_codes WHERE practice_id = ? AND patient_id = ? AND created_at >= ?",
                    (patient["practice_id"], patient["id"], since), 0)


# Abmelden / Zugang beenden: auch die Offline-Kopien (Startseite, Übungen) auf dem Telefon löschen – sonst blieben
# Gesundheitsdaten im Browser-Speicher, und offline sähe sie, wer das Telefon in der Hand hat.
CLEAR_DEVICE = '"cache", "storage"'


async def logout(request: web.Request) -> web.StreamResponse:
    destroy_session(request.app[DB], request.cookies.get(PATIENT_COOKIE))
    resp = web.HTTPFound("/p/anmelden")
    resp.del_cookie(PATIENT_COOKIE, path="/")
    resp.headers["Clear-Site-Data"] = CLEAR_DEVICE
    raise resp


# --------------------------------------------------------------------------- Patient: Seiten
@patient_required
async def home(request: web.Request) -> web.StreamResponse:
    """Startseite: Karten der eingeschalteten Module (Termin, Übungen, Info der Therapeutin, Nachrichten …)."""
    return await render(request, "patientapp/home.html", {"cards": await collect(request, "patient.home.card")})


@patient_required
async def profile(request: web.Request) -> web.StreamResponse:
    from ...core import languages

    p = request["patient"]
    return await render(request, "patientapp/profile.html", {
        "langs": PATIENT_LANGS, "spoken": languages.name_for(p["spoken_lang"], request["lang"]) if p["spoken_lang"] else None,
    })


@patient_required
async def revoke(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    p = request["patient"]
    db.execute("UPDATE patient_consents SET revoked_at = ? WHERE practice_id = ? AND patient_id = ? AND revoked_at IS NULL",
               (utcnow(), current_practice_id(), p["id"]))
    end_patient_sessions(db, current_practice_id(), p["id"])
    audit(db, "delete", "invite", None, p["id"], {"revoked": True})
    resp = web.HTTPFound("/p/anmelden")
    resp.del_cookie(PATIENT_COOKIE, path="/")
    resp.headers["Clear-Site-Data"] = CLEAR_DEVICE
    raise resp


async def manifest(request: web.Request) -> web.StreamResponse:
    lang = request["lang"]
    t = request.app[I18N].t
    return web.json_response({
        "name": t(lang, "p.app_name"), "short_name": "N-Fizyo", "start_url": "/p/heute", "scope": "/p/",
        "display": "standalone", "background_color": "#F9F9FF", "theme_color": "#2F5DA8", "lang": lang,
        "icons": [{"src": "/static/img/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
    }, content_type="application/manifest+json")


async def service_worker(request: web.Request) -> web.StreamResponse:
    js = (Path(__file__).parent / "sw.js").read_text(encoding="utf-8")
    return web.Response(text=js, content_type="application/javascript", headers={"Cache-Control": "no-cache"})


async def languages_page(request: web.Request) -> web.StreamResponse:
    nxt = request.query.get("next", "/p/anmelden")
    if not nxt.startswith("/p/"):
        nxt = "/p/anmelden"
    return await render(request, "patientapp/languages.html", {"langs": PATIENT_LANGS, "next": nxt})


async def privacy_page(request: web.Request) -> web.StreamResponse:
    """Datenschutzerklärung für Patient/innen (Informationspflicht nDSG). Verantwortlich ist die Praxis:
    mit App-Sitzung deren Praxis, sonst über ?praxis=<kurzname> (öffentliche Angaben der Praxis)."""
    db = request.app[DB]
    session = request.get("patient_session")
    practice = None
    if session is not None:
        practice = db.one("SELECT name, address, email, phone FROM practices WHERE id = ?", (session["practice_id"],))
    else:
        slug = request.query.get("praxis", "")[:60]
        if not slug:                                   # von der Online-Terminseite gekommen (gleiche Adresse): deren Praxis
            ref = urlparse(request.headers.get("Referer", ""))
            m = re.match(r"^/p/termin/([a-z0-9-]{1,60})", ref.path or "")
            if m and (not ref.netloc or ref.netloc == request.host):
                slug = m.group(1)
        if slug:
            practice = db.one("SELECT name, address, email, phone FROM practices WHERE slug = ?", (slug,))
    return await render(request, "patientapp/privacy.html", {"practice": practice, "path": request.path_qs})


async def p_root(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/p/heute" if request.get("patient") else "/p/anmelden")


def usage_app(request: web.Request) -> dict:
    n = request.app[DB].value("SELECT COUNT(DISTINCT patient_id) FROM patient_consents WHERE practice_id = ? AND kind = 'app'"
                              " AND revoked_at IS NULL", (current_practice_id(),), 0)
    return {"label_key": "stats.app.consents", "n": n}


def setup(ctx: ModuleContext) -> None:
    ctx.public("/p/", p_root)
    ctx.public("/p/einladung/{token}", invite_open, ("GET", "POST"))
    ctx.public("/p/anmelden", login, ("GET", "POST"))
    ctx.public("/p/abmelden", logout)
    ctx.public("/p/sprachen", languages_page)
    ctx.public("/p/datenschutz", privacy_page)
    ctx.public("/p/manifest.webmanifest", manifest)
    ctx.public("/p/sw.js", service_worker)
    ctx.patient_page("/heute", home)
    ctx.patient_page("/profil", profile)
    ctx.patient_page("/profil/zugang-beenden", revoke, ("POST",))
    ctx.nav("patient", NavItem("p.nav.today", "home", "/heute", order=10, short_key="p.nav.today_short"))
    for screen in ("therapie", "empfang"):
        ctx.page(screen, "/patienten/{pid:\\d+}/app-einladung", invite_create, ("POST",))
        ctx.page(screen, "/patienten/{pid:\\d+}/app-abmelden", app_logout, ("POST",))
    ctx.slot("therapie.patient.action", 80, action_invite)
    ctx.slot("empfang.patient.action", 10, action_invite)
    # nur am Empfang: in der Therapie wäre ein weiteres Symbol (Pfeil aus der Tür) leicht mit «selbst abmelden» zu verwechseln
    ctx.slot("empfang.patient.action", 12, action_app_logout)
    ctx.slot("leitung.overview.usage", 10, usage_app)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('patient_invites', 'patient_consents', 'patient_login_codes'), (
        "UPDATE patient_invites SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE patient_consents SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE patient_login_codes SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
