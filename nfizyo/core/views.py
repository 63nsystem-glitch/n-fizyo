"""Kern-Seiten: Anmelden, Bildschirm wählen, Mein Konto, Abmelden, Sprache, Gesundheitscheck."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from urllib.parse import quote, urlparse

from aiohttp import web

from . import mfa, totp
from .audit import audit
from .auth import MIN_PASSWORD, STAFF_SESSION_DAYS, authenticate, create_session, destroy_session, initials_of
from .i18n import PATIENT_LANGS, STAFF_LANGS
from .keys import DB, SETTINGS, THROTTLE
from .screens import SCREENS, screen_page, screens_for
from .site_content import SITE, site_texts
from .security import hash_password, sign, token_hash, unsign, verify_password
from .tenant import practice_scope
from .web import (LANG_COOKIE, PATIENT_COOKIE, SCREEN_COOKIE, STAFF_COOKIE, client_ip, login_required, redirect_with_flash,
                  render)

# Bremsen beim Anmelden (core/auth.py LoginThrottle), alle mit der echten Adresse (client_ip) bzw. unabhängig davon:
LOGIN_PAIR_LIMIT = 8        # Adresse + E-Mail: 8 Fehlversuche in 15 Minuten
LOGIN_IP_LIMIT = 30         # Adresse allein (alle Konten): 30 Fehlversuche in 15 Minuten
ACCOUNT_WINDOW = 3600       # Konto (E-Mail, egal von wo): ab dem 6. Fehlversuch in 1 h wachsende Pause, höchstens 15 Min.
MFA_LIMIT = 8               # Code aus der App: 8 Fehlversuche in 15 Minuten pro Konto
# Bekanntes Gerät: Browser, in dem sich die Person schon erfolgreich angemeldet hat. Die Konto-Pause gilt dort nicht –
# so kann niemand ein Konto von aussen aussperren (die Bremsen pro Adresse gelten weiter).
DEVICE_COOKIE = "nf_dev"
DEVICE_DAYS = 180
DEVICE_MAX = 5


def _safe_next(value: str | None, default: str) -> str:
    if not value:
        return default
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return default
    return value


def security_url(user, last_screen: str | None = None) -> str:
    """Mein Konto › Sicherheit im passenden Bildschirm."""
    mine = screens_for(user)
    chosen = next((x for x in mine if x.key == last_screen), mine[0] if mine else None)
    return (chosen.prefix if chosen else "") + "/konto/sicherheit"


def home_for(user, last_screen: str | None = None) -> str:
    """Eine Rolle → direkt in den Bildschirm. Mehrere → zuletzt benutzter, sonst Auswahl."""
    if user is None:
        return "/login"
    mine = screens_for(user)
    if len(mine) == 1:
        return mine[0].prefix + mine[0].home
    for s in mine:
        if s.key == last_screen:
            return s.prefix + s.home
    return "/start"


async def index(request: web.Request) -> web.StreamResponse:
    # Demo-Server: Besucher ohne Anmeldung sehen die Vorstellungsseite. Eine echte Praxis zeigt hier immer die Anmeldung.
    if request.get("user") is None and request.app[SETTINGS].public_site:
        lang = request.get("lang", "de")
        lang = lang if lang in SITE else "de"
        return await render(request, "core/site.html", {"s": site_texts(lang), "site_lang": lang,
                                                        "site_contact": request.app[SETTINGS].site_contact})
    raise web.HTTPFound(home_for(request.get("user"), request.cookies.get(SCREEN_COOKIE)))


async def login_page(request: web.Request) -> web.StreamResponse:
    if request.get("user") is not None:
        raise web.HTTPFound(home_for(request["user"], request.cookies.get(SCREEN_COOKIE)))
    return await render(request, "core/login.html", {"next": request.query.get("next", ""), "error": None, "email": ""})


def _device_tag(secret: bytes, email: str) -> str:
    return hmac.new(secret, b"device:" + email.strip().lower().encode(), hashlib.sha256).hexdigest()[:20]


def _device_tags(request: web.Request) -> list[str]:
    raw = request.cookies.get(DEVICE_COOKIE)
    data = unsign(request.app[SETTINGS].secret_key, "device", raw) if raw else None
    tags = data.get("h") if data else None
    return [t for t in tags if isinstance(t, str)][:DEVICE_MAX] if isinstance(tags, list) else []


async def login_submit(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    email = str(form.get("email", "")).strip()[:200]
    password = str(form.get("password", ""))[:1024]
    next_url = str(form.get("next", ""))
    throttle = request.app[THROTTLE]
    settings = request.app[SETTINGS]
    ip = client_ip(request)
    k_pair, k_ip, k_acct = f"login:{ip}:{email.lower()}", f"login-ip:{ip}", f"login-acct:{email.lower()}"
    known_device = _device_tag(settings.secret_key, email) in _device_tags(request)
    db = request.app[DB]
    if (throttle.blocked(k_pair, LOGIN_PAIR_LIMIT) or throttle.blocked(k_ip, LOGIN_IP_LIMIT)
            or (not known_device and throttle.delay(k_acct, window=ACCOUNT_WINDOW))):
        return await render(request, "core/login.html", {"next": next_url, "error": "login.blocked", "email": email}, status=429)
    # scrypt braucht ~0,4 s: im Hintergrund-Thread, damit andere Anfragen nicht warten
    user = await asyncio.to_thread(authenticate, db, email, password)
    if user is None or not screens_for(user):
        throttle.fail(k_pair)
        throttle.fail(k_ip)
        throttle.fail(k_acct, window=ACCOUNT_WINDOW)
        return await render(request, "core/login.html", {"next": next_url, "error": "login.failed", "email": email}, status=401)
    throttle.reset(k_pair)
    throttle.reset(k_acct)
    destroy_session(db, request.cookies.get(STAFF_COOKIE))
    if mfa.enabled(user):
        stage = "code"
    elif settings.require_2fa:
        stage = "setup"
    else:
        stage = "ok"
    if stage != "ok":
        token, _ = create_session(db, "staff", user_id=user["id"], practice_id=user["practice_id"], stage=stage)
        target = "/login/code" if stage == "code" else "/sicherheit/einrichten"
        resp = web.HTTPFound(target + ("?next=" + quote(_safe_next(next_url, ""), safe="") if _safe_next(next_url, "") else ""))
        resp.set_cookie(STAFF_COOKIE, token, httponly=True, samesite="Lax", secure=settings.secure_cookies)
        raise resp
    raise _finish_login(request, user, next_url)


def _finish_login(request: web.Request, user, next_url: str = "", *, method: str = "password") -> web.HTTPFound:
    """Anmeldung abschliessen: neue Sitzung (neues Token – auch nach der Zwei-Schritt-Prüfung), Audit, Weiterleitung."""
    db = request.app[DB]
    settings = request.app[SETTINGS]
    destroy_session(db, request.cookies.get(STAFF_COOKIE))
    token, _ = create_session(db, "staff", user_id=user["id"], practice_id=user["practice_id"])
    with practice_scope(user["practice_id"], user["id"]):
        audit(db, "login", "user", user["id"], None, {"method": method})
    resp = web.HTTPFound(_safe_next(next_url, home_for(user, request.cookies.get(SCREEN_COOKIE))))
    resp.set_cookie(STAFF_COOKIE, token, httponly=True, samesite="Lax", secure=settings.secure_cookies,
                    max_age=STAFF_SESSION_DAYS * 24 * 3600)
    tag = _device_tag(settings.secret_key, user["email"])
    tags = [tag] + [t for t in _device_tags(request) if t != tag]
    resp.set_cookie(DEVICE_COOKIE, sign(settings.secret_key, "device", {"h": tags[:DEVICE_MAX]}, DEVICE_DAYS * 86400),
                    httponly=True, samesite="Lax", secure=settings.secure_cookies, max_age=DEVICE_DAYS * 86400)
    return resp


def _pending(request: web.Request, stage: str):
    """Zwischenschritt der Anmeldung (Passwort war richtig). Sonst zurück zur Anmeldung."""
    user = request.get("pending_user")
    session = request.get("session")
    if user is None or session is None or session["stage"] != stage:
        raise web.HTTPFound("/login")
    return user


async def login_code(request: web.Request) -> web.StreamResponse:
    """Schritt 2: 6-stelliger Code aus der Authenticator-App (oder ein Wiederherstellungscode)."""
    user = _pending(request, "code")
    next_url = request.query.get("next", "")
    error = None
    if request.method == "POST":
        form = await request.post()
        next_url = str(form.get("next", ""))
        throttle = request.app[THROTTLE]
        key = f"mfa:{user['id']}"   # pro Konto, unabhängig von der Adresse (das Passwort stimmte schon)
        if throttle.blocked(key, MFA_LIMIT):
            error = "login.blocked"
        else:
            how = mfa.check(request.app[DB], request.app[SETTINGS].secret_key, user, str(form.get("code", "")))
            if how:
                throttle.reset(key)
                if how == "recovery":
                    raise _finish_login(request, user, security_url(user, request.cookies.get(SCREEN_COOKIE)) + "?recovery=1",
                                        method="recovery")
                raise _finish_login(request, user, next_url, method="totp")
            throttle.fail(key)
            error = "mfa.code_wrong"
    return await render(request, "core/login_code.html", {"next": next_url, "error": error})


async def security_setup(request: web.Request) -> web.StreamResponse:
    """Zwei-Schritt-Anmeldung einrichten: beim ersten Anmelden (Pflicht) oder später aus «Mein Konto» (neues Telefon)."""
    db = request.app[DB]
    settings = request.app[SETTINGS]
    user = request.get("user")
    first_time = user is None
    renew = ""
    if first_time:
        user = _pending(request, "setup")
    else:
        # Angemeldet (neues Telefon): nur mit frischer Passwort-Bestätigung (signiertes Kurzzeit-Token aus «Mein Konto»)
        form0 = await request.post() if request.method == "POST" else None
        renew = str((form0.get("t") if form0 is not None else request.query.get("t")) or "")
        claim = unsign(settings.secret_key, "mfa-renew", renew) if renew else None
        if not claim or claim.get("u") != user["id"]:
            raise web.HTTPFound(security_url(user, request.cookies.get(SCREEN_COOKIE)))
    next_url = request.query.get("next", "")
    error = None
    if request.method == "POST":
        form = await request.post()
        next_url = str(form.get("next", ""))
        throttle = request.app[THROTTLE]
        key = f"mfa:{user['id']}"
        fresh = db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
        if throttle.blocked(key, MFA_LIMIT):
            error = "login.blocked"
        else:
            codes = mfa.confirm_setup(db, settings.secret_key, fresh, str(form.get("code", "")))
            if codes:
                throttle.reset(key)
                resp = await render(request, "core/recovery_codes.html", {
                    "codes": codes, "continue_url": _safe_next(next_url, home_for(fresh, request.cookies.get(SCREEN_COOKIE))),
                    "first": True})
                resp.headers["Cache-Control"] = "no-store"
                # Sitzung erst jetzt zur vollen Anmeldung machen (neues Token)
                login = _finish_login(request, fresh, next_url, method="mfa_setup")
                for cookie in login.cookies.values():
                    resp.cookies[cookie.key] = cookie
                return resp
            throttle.fail(key)
            error = "mfa.code_wrong"
    fresh = db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
    secret = mfa.pending_secret(settings.secret_key, fresh) if request.method == "POST" or not request.query.get("neu") else None
    if not secret:
        secret = mfa.begin_setup(db, settings.secret_key, user["id"])
    uri = totp.otpauth_uri(secret, user["email"])
    resp = await render(request, "core/mfa_setup.html", {
        "qr": _qr_svg(uri), "secret": totp.pretty_secret(secret), "next": next_url, "error": error, "first_time": first_time,
        "renew": renew})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _qr_svg(data: str) -> str:
    import segno

    return segno.make(data, error="m").svg_inline(scale=5, dark="#191C20", light="#FFFFFF", border=2)


async def account_security(request: web.Request) -> web.StreamResponse:
    """Mein Konto › Sicherheit: Zwei-Schritt-Anmeldung, Wiederherstellungscodes, neues Telefon."""
    db = request.app[DB]
    user = request["user"]
    settings = request.app[SETTINGS]
    here = request["screen"].prefix + "/konto/sicherheit"
    error = None
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "codes":
            if mfa.check(db, settings.secret_key, user, str(form.get("code", ""))) != "totp":
                error = "mfa.code_wrong"
            else:
                resp = await render(request, "core/recovery_codes.html", {
                    "codes": mfa.new_recovery_codes(db, user), "continue_url": here, "first": False})
                resp.headers["Cache-Control"] = "no-store"
                return resp
        elif do == "new_phone":
            if not await asyncio.to_thread(verify_password, str(form.get("password", ""))[:1024], user["password_hash"]):
                error = "account.pw_wrong"
            else:
                token = sign(settings.secret_key, "mfa-renew", {"u": user["id"]}, 600)
                audit(db, "mfa_renew_started", "user", user["id"])
                raise web.HTTPFound("/sicherheit/einrichten?neu=1&t=" + quote(token, safe="") + "&next=" + quote(here, safe=""))
    return await render(request, "core/account_security.html", {
        "enabled": mfa.enabled(user), "left": mfa.recovery_left(user), "since": user["totp_enabled_at"],
        "error": error, "recovery_used": request.query.get("recovery") == "1", "required": settings.require_2fa})


@login_required
async def start(request: web.Request) -> web.StreamResponse:
    """Bildschirm wählen (für Personen mit mehreren Rollen, z. B. Leitung, die selbst behandelt)."""
    mine = screens_for(request["user"])
    key = request.query.get("s")
    for s in mine:
        if s.key == key:
            resp = web.HTTPFound(s.prefix + s.home)
            resp.set_cookie(SCREEN_COOKIE, s.key, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
            raise resp
    if len(mine) == 1:
        raise web.HTTPFound(mine[0].prefix + mine[0].home)
    return await render(request, "core/start.html", {"screens": mine})


async def account(request: web.Request) -> web.StreamResponse:
    """Mein Konto – in jedem Personal-Bildschirm unter …/konto."""
    db = request.app[DB]
    user = request["user"]
    here = request["screen"].prefix + "/konto"
    error = None
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "profile":
            name = str(form.get("name") or "").strip()[:120]
            lang = str(form.get("lang") or "")
            if name and lang in STAFF_LANGS:
                db.execute("UPDATE users SET name = ?, initials = ?, lang = ? WHERE id = ?", (name, initials_of(name), lang, user["id"]))
                resp = redirect_with_flash(here, "common.saved")
                resp.set_cookie(LANG_COOKIE, lang, httponly=True, samesite="Lax", secure=request.app[SETTINGS].secure_cookies)
                raise resp
        elif form.get("do") == "password":
            old, new, rep = str(form.get("current") or ""), str(form.get("new") or ""), str(form.get("repeat") or "")
            if not await asyncio.to_thread(verify_password, old[:1024], user["password_hash"]):
                error = "account.pw_wrong"
            elif len(new) < MIN_PASSWORD:
                error = "account.pw_short"
            elif new != rep:
                error = "account.pw_mismatch"
            else:
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (await asyncio.to_thread(hash_password, new), user["id"]))
                db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                           (user["id"], token_hash(request.cookies.get(STAFF_COOKIE, ""))))  # andere Anmeldungen beenden
                audit(db, "password_change", "user", user["id"])
                raise redirect_with_flash(here, "account.pw_changed")
    return await render(request, "core/account.html", {"error": error, "min_pw": MIN_PASSWORD})


async def logout(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    if request.get("user") is not None:
        audit(db, "logout", "user", request["user"]["id"])
    destroy_session(db, request.cookies.get(STAFF_COOKIE))
    resp = web.HTTPFound("/login")
    resp.del_cookie(STAFF_COOKIE)
    raise resp


async def set_lang(request: web.Request) -> web.StreamResponse:
    code = request.match_info["code"]
    area = request.query.get("area", "staff")
    allowed = PATIENT_LANGS if area == "patient" else STAFF_LANGS
    if code not in allowed:
        raise web.HTTPNotFound()
    db = request.app[DB]
    default = "/p/heute" if area == "patient" else home_for(request.get("user"), request.cookies.get(SCREEN_COOKIE))
    resp = web.HTTPFound(_safe_next(request.query.get("next"), default))
    if area == "patient":
        from .auth import load_session

        session = load_session(db, request.cookies.get(PATIENT_COOKIE))
        if session is not None and session["kind"] == "patient":
            db.execute("UPDATE patients SET lang = ? WHERE id = ? AND practice_id = ?",
                       (code, session["patient_id"], session["practice_id"]))
    elif request.get("user") is not None:
        db.execute("UPDATE users SET lang = ? WHERE id = ?", (code, request["user"]["id"]))
    resp.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    raise resp


async def health(request: web.Request) -> web.StreamResponse:
    """Für Überwachung und Update-Skript: Datenbank erreichbar + laufende Programmversion."""
    from .. import __version__

    request.app[DB].value("SELECT 1")
    return web.json_response({"status": "ok", "version": __version__})


async def dismiss_notice(request: web.Request) -> web.StreamResponse:
    """Wartungshinweis von NSystem für diese Person ausblenden (pro Person, gilt auf allen Bildschirmen)."""
    from . import nsystem

    form = await request.post()
    nsystem.dismiss_notice(request.app[DB], request.match_info["nid"], request["user"])
    nxt = str(form.get("next") or "")
    home = request["screen"].prefix + request["screen"].home
    raise web.HTTPFound(nxt if nxt.startswith(request["screen"].prefix + "/") and "//" not in nxt and "\\" not in nxt else home)


def setup(app: web.Application) -> None:
    r = app.router
    r.add_get("/", index)
    r.add_get("/login", login_page)
    r.add_post("/login", login_submit)
    r.add_post("/logout", logout)
    r.add_get("/start", start)
    r.add_get("/login/code", login_code)
    r.add_post("/login/code", login_code)
    r.add_get("/sicherheit/einrichten", security_setup)
    r.add_post("/sicherheit/einrichten", security_setup)
    r.add_get("/lang/{code}", set_lang)
    r.add_get("/health", health)
    for key, screen in SCREENS.items():
        handler = screen_page(key, None, account)
        r.add_get(screen.prefix + "/konto", handler)
        r.add_post(screen.prefix + "/konto", handler)
        sec = screen_page(key, None, account_security)
        r.add_get(screen.prefix + "/konto/sicherheit", sec)
        r.add_post(screen.prefix + "/konto/sicherheit", sec)
        r.add_post(screen.prefix + "/hinweise/{nid}/ausblenden", screen_page(key, None, dismiss_notice))
