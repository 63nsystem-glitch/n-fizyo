"""Web-Schicht: Anfrage-Kontext, Rollenprüfung, CSRF, Seitenaufbau."""
from __future__ import annotations

import functools
import hmac
import ipaddress
import logging
import re
import weakref
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp_jinja2
import jinja2
from aiohttp import web
from markupsafe import Markup, escape

from . import formats, timeutil
from .auth import load_session
from .i18n import LANG_NAMES, PATIENT_LANGS, STAFF_LANGS, from_accept_language, pick_lang, text_dir
from .icons_data import ICONS
from .keys import AI, BADGES, DB, I18N, MODULES, SETTINGS, STATIC_VERSION
from .modules import module_enabled
from .screens import NAV, prefix as screen_prefix, roles_of, screens_for
from .security import new_token
from .tenant import practice_scope

log = logging.getLogger(__name__)

STAFF_COOKIE = "nf_s"
PATIENT_COOKIE = "nf_p"
ANON_CSRF_COOKIE = "nf_csrf"
LANG_COOKIE = "nf_lang"
SCREEN_COOKIE = "nf_screen"
FLASH_COOKIE = "nf_flash"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# --------------------------------------------------------------------------- Kontext
@web.middleware
async def context_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    app = request.app
    db = app[DB]
    settings = app[SETTINGS]
    request["user"] = None
    request["patient_session"] = None
    request["patient"] = None
    is_patient_area = request.path == "/p" or request.path.startswith("/p/")

    session = None
    if is_patient_area:
        session = load_session(db, request.cookies.get(PATIENT_COOKIE))
        if session is not None and session["kind"] == "patient":
            patient = db.one(
                "SELECT * FROM patients WHERE id = ? AND practice_id = ? AND archived = 0",
                (session["patient_id"], session["practice_id"]),
            )
            if patient is not None:
                request["patient_session"] = session
                request["patient"] = patient
            else:
                session = None
        else:
            session = None
    else:
        request["pending_user"] = None
        session = load_session(db, request.cookies.get(STAFF_COOKIE), idle_hours=getattr(settings, "staff_idle_hours", None))
        if session is not None and session["kind"] == "staff":
            row = db.one("SELECT * FROM users WHERE id = ? AND active = 1", (session["user_id"],))
            if row is None:
                session = None
            elif session["stage"] != "ok":
                # Passwort stimmt, Zwei-Schritt-Anmeldung noch offen: NICHT angemeldet, nur /login/code bzw. /sicherheit/einrichten
                request["pending_user"] = row
            else:
                user = dict(row)
                user["role_set"] = roles_of(row)
                request["user"] = user
        else:
            session = None
    request["session"] = session
    user = request["user"]
    patient = request["patient"]

    # Sprache: Personal DE/FR, Patienten-App zusätzlich weitere Sprachen.
    # Öffentliche Seiten (nicht angemeldet) nehmen ?lang=xx an und merken es sich – so kann die Praxis einen Link in einer
    # bestimmten Sprache verschicken (z. B. /p/termin/praxis?lang=tr).
    accept = request.headers.get("Accept-Language")
    url_lang = None
    if user is None and patient is None and request.query.get("lang"):
        url_lang = pick_lang(request.query.get("lang"), default="", allowed=PATIENT_LANGS if is_patient_area else STAFF_LANGS) or None
    if is_patient_area:
        request["lang"] = url_lang or pick_lang(
            patient["lang"] if patient else request.cookies.get(LANG_COOKIE),
            from_accept_language(accept, PATIENT_LANGS),
            default=settings.default_lang,
            allowed=PATIENT_LANGS,
        )
    else:
        request["lang"] = url_lang or pick_lang(
            user["lang"] if user else request.cookies.get(LANG_COOKIE),
            from_accept_language(accept, STAFF_LANGS),
            default=settings.default_lang,
            allowed=STAFF_LANGS,
        )

    # CSRF
    new_anon_csrf = None
    if session is not None:
        csrf = session["csrf"]
    else:
        csrf = request.cookies.get(ANON_CSRF_COOKIE)
        if not csrf:
            csrf = new_anon_csrf = new_token(16)
    request["csrf"] = csrf
    if request.method in UNSAFE and not getattr(_route_handler(request), "csrf_exempt", False):
        sent = request.headers.get("X-CSRF-Token")
        if not sent:
            form = await request.post()
            sent = form.get("_csrf")
        if not sent or not hmac.compare_digest(str(sent), csrf):
            raise web.HTTPForbidden(text="CSRF-Prüfung fehlgeschlagen. Bitte Seite neu laden.")

    practice_id = None
    if user is not None:
        practice_id = user["practice_id"]
    elif request["patient_session"] is not None:
        practice_id = request["patient_session"]["practice_id"]

    def finish(resp: web.StreamResponse) -> web.StreamResponse:
        if new_anon_csrf:
            resp.set_cookie(ANON_CSRF_COOKIE, new_anon_csrf, httponly=True, samesite="Lax", secure=settings.secure_cookies)
        if url_lang and request.cookies.get(LANG_COOKIE) != url_lang:
            resp.set_cookie(LANG_COOKIE, url_lang, max_age=365 * 86400, httponly=True, samesite="Lax", secure=settings.secure_cookies)
        if request.get("_flash_read"):
            resp.del_cookie(FLASH_COOKIE)
        if isinstance(resp, web.Response) and not isinstance(resp, web.HTTPException) and resp.body is not None:
            ctype = resp.content_type or ""
            if ctype.startswith(("text/", "application/json", "image/svg")) and len(resp.body) > 2048:
                resp.enable_compression()
        _security_headers(request, resp)
        return resp

    with practice_scope(practice_id, user["id"] if user else None, patient["id"] if patient else None):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            finish(exc)
            raise
    return finish(response)


def _route_handler(request: web.Request):
    route = request.match_info.route
    return getattr(route, "handler", None)


def csrf_exempt(handler: Handler) -> Handler:
    handler.csrf_exempt = True  # type: ignore[attr-defined]
    return handler


def _error_ctx(request: web.Request, status: int) -> dict[str, Any]:
    """Fehlerseite: im Patienten-Bereich (/p) in der Sprache der Patientin / des Patienten und mit Link zur App
    (/p/heute, abgemeldet /p/anmelden) – nie zur Anmeldung des Personals."""
    if request.path == "/p" or request.path.startswith("/p/"):
        return {"status": status, "patient_area": True, "home_href": "/p/heute" if request.get("patient") is not None else "/p/anmelden"}
    return {"status": status, "patient_area": False, "home_href": "/"}


def _scope_of(request: web.Request) -> tuple[int | None, int | None, int | None]:
    """(Praxis, Person, Patient/in) der Anfrage – wie im context_middleware festgelegt."""
    user, ps, patient = request.get("user"), request.get("patient_session"), request.get("patient")
    if user is not None:
        return user["practice_id"], user["id"], None
    if ps is not None:
        return ps["practice_id"], None, patient["id"] if patient is not None else None
    return None, None, None


async def _error_page(request: web.Request, status: int) -> web.Response:
    """Fehlerseite im Rahmen der eigenen Praxis rendern (Menü mit Zählern) – der Handler hat den Praxis-Rahmen schon verlassen."""
    with practice_scope(*_scope_of(request)):
        return await render(request, "errors/error.html", _error_ctx(request, status), status=status)


@web.middleware
async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status in (403, 404) and "text/html" in request.headers.get("Accept", "text/html") and exc.content_type != "application/json":
            try:
                return await _error_page(request, exc.status)
            except Exception:  # pragma: no cover - Fallback
                log.exception("Fehlerseite %s bei %s", exc.status, request.path)
                raise exc
        raise
    except OverflowError:
        # Riesige Zahl in Adresse oder Formular (z. B. ?patient=99999999999999999999): SQLite kann sie nicht
        # speichern. Das ist eine unsinnige Eingabe, kein Serverfehler → «nicht gefunden» statt 500.
        log.warning("Zahl zu gross bei %s %s", request.method, request.path)
        if request.headers.get("Accept", "").startswith("application/json"):
            return web.json_response({"error": "not_found"}, status=404)
        try:
            return await _error_page(request, 404)
        except Exception:  # pragma: no cover
            return web.Response(text="Nicht gefunden / Introuvable", status=404)
    except Exception:
        log.exception("Unerwarteter Fehler bei %s %s", request.method, request.path)
        if request.headers.get("Accept", "").startswith("application/json"):
            return web.json_response({"error": "server"}, status=500)
        try:
            return await _error_page(request, 500)
        except Exception:  # pragma: no cover - die Fehlerseite selbst darf nie einen zweiten Fehler zeigen
            log.exception("Fehlerseite 500 bei %s", request.path)
            return web.Response(text="Interner Fehler / Erreur interne", status=500)
    finally:
        _remember_public_base(request)


# --------------------------------------------------------------------------- Echte Adresse hinter dem Proxy
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")


@functools.lru_cache(maxsize=32)
def _networks(entries: tuple[str, ...]) -> tuple:
    nets = []
    for e in entries:
        try:
            nets.append(ipaddress.ip_network(e.strip(), strict=False))
        except ValueError:
            log.warning("NFIZYO_TRUSTED_PROXIES: ungültiger Eintrag %r wird ignoriert", e)
    return tuple(nets)


def _ip(value: str | None):
    if not value:
        return None
    v = value.strip()
    if v.startswith("[") and "]" in v:          # [2001:db8::1]:443
        v = v[1:v.index("]")]
    elif v.count(":") == 1 and "." in v:         # 203.0.113.5:443
        v = v.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def client_ip(request: web.Request, trusted: tuple[str, ...] | None = None) -> str:
    """Adresse der Besucherin / des Besuchers – für Versuchs-Bremsen (LoginThrottle).

    Im Betrieb steht Caddy davor (reverse_proxy 127.0.0.1:8080): request.remote ist dann immer 127.0.0.1. Nur wenn die
    Verbindung von einem vertrauenswürdigen Proxy kommt (Settings.trusted_proxies, Standard 127.0.0.1 und ::1), gilt
    X-Forwarded-For – von rechts gelesen, der erste Eintrag, der kein vertrauenswürdiger Proxy ist (den hat unser Proxy
    selbst angehängt). Schickt jemand anderes den Kopf mit, wird er ignoriert (sonst könnte man jede Bremse umgehen)."""
    remote = request.remote or ""
    if trusted is None:
        settings = request.app.get(SETTINGS) if SETTINGS in request.app else None
        trusted = tuple(getattr(settings, "trusted_proxies", DEFAULT_TRUSTED_PROXIES))
    nets = _networks(tuple(trusted))
    remote_ip = _ip(remote)
    if remote_ip is None or not any(remote_ip in n for n in nets):
        return str(remote_ip) if remote_ip is not None else (remote or "unbekannt")
    hops = [h for value in request.headers.getall("X-Forwarded-For", []) for h in value.split(",") if h.strip()]
    for hop in reversed(hops):
        ip = _ip(hop)
        if ip is None:
            break                                  # kaputter Eintrag: nichts weiter links glauben
        if any(ip in n for n in nets):
            continue
        return str(ip)
    return str(remote_ip)


# --------------------------------------------------------------------------- Öffentliche Adresse für Links
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def public_url(request: web.Request | None, path: str = "", settings=None) -> str:
    """Link nach aussen (QR, SMS, Einladung, Online-Termine): immer aus der eingestellten Adresse der Praxis
    (Settings.base_url). Ist keine öffentliche Adresse eingestellt (Standard 127.0.0.1:8080, z. B. Demo), gilt die
    Adresse, unter der die Seite gerade geöffnet ist – nie ein fest eingebautes localhost mit Port."""
    from urllib.parse import urlparse

    if settings is None and request is not None:
        settings = request.app[SETTINGS]
    base = (getattr(settings, "base_url", "") or "").rstrip("/")
    host = urlparse(base).hostname or ""
    if (not base or host in _LOOPBACK) and request is not None and request.host:
        base = f"{request.scheme}://{request.host}"
    return base + path


_PUBLIC_BASES: "weakref.WeakKeyDictionary[web.Application, dict[int, str]]" = weakref.WeakKeyDictionary()


def _remember_public_base(request: web.Request) -> None:
    """Adresse merken, unter der angemeldete Personen einer Praxis (Personal oder Patienten-App) den Server gerade erreichen –
    für Links aus Hintergrundaufgaben (Benachrichtigungen), wenn keine öffentliche Adresse eingestellt ist."""
    try:
        practice_id = _scope_of(request)[0]
        if practice_id is None or not request.host:
            return
        _PUBLIC_BASES.setdefault(request.app, {})[practice_id] = f"{request.scheme}://{request.host}"
    except Exception:  # pragma: no cover - nie die Antwort stören
        log.debug("Adresse nicht gemerkt", exc_info=True)


def background_public_url(app: web.Application, practice_id: int | None, path: str = "") -> str:
    """Wie public_url, aber ohne Anfrage (Versand im Hintergrund): eingestellte öffentliche Adresse; sonst die Adresse, unter der
    die Praxis den Server zuletzt geöffnet hat (gleiche Regel wie public_url); sonst die eingestellte Adresse mit dem Port,
    auf dem der Server wirklich läuft – nie ein fest eingebautes 127.0.0.1:8080."""
    from urllib.parse import urlparse

    settings = app[SETTINGS]
    base = (getattr(settings, "base_url", "") or "").rstrip("/")
    u = urlparse(base)
    if base and (u.hostname or "") not in _LOOPBACK:
        return base + path
    seen = _PUBLIC_BASES.get(app) or {}
    if practice_id in seen:
        return seen[practice_id] + path
    if seen:
        return list(seen.values())[-1] + path
    port = getattr(settings, "port", None)
    if u.hostname and port and u.port != port:
        base = f"{u.scheme or 'http'}://{u.hostname}:{port}"
    return (base or f"http://127.0.0.1:{port or 8080}") + path


def link_is_public(url: str) -> bool:
    """Ist der Link von aussen erreichbar (z. B. Telefon der Patientin zu Hause)? Nur https mit Internet-Namen –
    nicht 127.0.0.1, localhost oder eine Adresse im Praxisnetz."""
    import ipaddress
    from urllib.parse import urlparse

    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or host in _LOOPBACK or "." not in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return not host.endswith((".local", ".lan", ".internal"))
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


# --------------------------------------------------------------------------- Zugriff
def login_required(handler: Handler) -> Handler:
    """Irgendein angemeldetes Praxiskonto (z. B. Bildschirmwahl, Mein Konto)."""
    @functools.wraps(handler)
    async def wrapper(request: web.Request):
        if request.get("user") is None:
            raise web.HTTPFound("/login?next=" + quote(request.path_qs, safe=""))
        return await handler(request)

    return wrapper


def patient_required(handler: Handler) -> Handler:
    @functools.wraps(handler)
    async def wrapper(request: web.Request):
        if request.get("patient") is None:
            raise web.HTTPFound("/p/anmelden")
        return await handler(request)

    return wrapper


def int_param(request: web.Request, name: str) -> int:
    try:
        value = int(request.match_info[name])
    except (KeyError, ValueError):
        raise web.HTTPNotFound()
    if not 0 <= value < 2 ** 63:   # SQLite INTEGER: größere Zahlen ergäben einen Serverfehler
        raise web.HTTPNotFound()
    return value


def form_int(form, name: str) -> int | None:
    """Ganzzahl aus einem Formularfeld (z. B. eine ID) oder None. Wie int_param: über 2^63 ergäbe sonst einen Serverfehler."""
    raw = str(form.get(name) or "").strip()
    if not raw.isdigit() or len(raw) > 18:
        return None
    return int(raw)


def redirect(location: str) -> web.HTTPFound:
    return web.HTTPFound(location)


def flash(response: web.StreamResponse, key: str) -> web.StreamResponse:
    if re.fullmatch(r"[a-z0-9_.]+", key):
        response.set_cookie(FLASH_COOKIE, key, max_age=60, httponly=True, samesite="Lax")
    return response


def redirect_with_flash(location: str, key: str) -> web.HTTPFound:
    exc = web.HTTPFound(location)
    flash(exc, key)
    return exc


# --------------------------------------------------------------------------- Sicherheits-Kopfzeilen
def content_security_policy(settings) -> str:
    """Nur eigene Skripte (kein Inline-JavaScript), keine fremden Quellen. Formulare dürfen nur zur eigenen Adresse –
    und zum eigenen Video-Server (Weiterleitung nach dem Beitreten)."""
    from urllib.parse import urlparse

    form = "'self'"
    video = getattr(settings, "video_url", "") or ""
    if video:
        u = urlparse(video)
        if u.scheme in ("https", "http") and u.netloc:
            form += f" {u.scheme}://{u.netloc}"
    return ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; connect-src 'self'; font-src 'self'; worker-src 'self'; manifest-src 'self'; "
            f"object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action {form}")


def _security_headers(request: web.Request, resp: web.StreamResponse) -> None:
    settings = request.app[SETTINGS]
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", content_security_policy(settings))
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=(), payment=(), usb=()")
    if settings.secure_cookies:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


# --------------------------------------------------------------------------- Vorlagen
def icon(name: str, size: int = 24, cls: str = "", label: str | None = None) -> Markup:
    d = ICONS.get(name) or ICONS["help"]
    aria = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"'
    return Markup(
        f'<svg class="ic {escape(cls)}" width="{size}" height="{size}" viewBox="0 -960 960 960" '
        f'fill="currentColor" {aria}><path d="{d}"/></svg>'
    )


def _format_ahv(value) -> str:
    from .patients import format_ahv

    return format_ahv(value)


def setup_templates(app: web.Application, dirs: list) -> jinja2.Environment:
    env = aiohttp_jinja2.setup(
        app,
        loader=jinja2.ChoiceLoader([jinja2.FileSystemLoader(str(d)) for d in dirs]),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined if app.get("_strict_templates") else jinja2.Undefined,
    )
    def num(value, digits: int = 1):
        """Zahl ohne unnötige Nachkommastellen: 3.0 → 3, 2.5 → 2.5"""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return value
        return str(int(round(f))) if abs(f - round(f)) < 1e-9 else f"{f:.{digits}f}"

    env.filters["num"] = num

    # Schweizer Formate (core/formats.py) – überall dieselben Helfer: Geld, Telefon, Namen, Prozent, Zeitraum
    env.globals["chf"] = formats.chf                       # {{ chf(1290) }} → CHF 1’290.00
    env.filters["money"] = formats.money                   # {{ 1290 | money }} → 1’290.00 (Spalte «CHF»)
    env.filters["phone"] = formats.phone                   # {{ '+41790000099' | phone }} → 079 000 00 99
    env.globals["person"] = formats.person_name            # {{ person(p) }} → Peter Muster
    env.globals["person_sorted"] = lambda p, **kw: formats.person_name(p, sorted_=True, **kw)   # → Muster, Peter

    @jinja2.pass_context
    def law(ctx, code):
        """Gesetz in der Sprache der Oberfläche: {{ p.law | law }} → KVG / LAMal, «privat» → Selbstzahler / Autopayeur."""
        if not code:
            return ""
        t = ctx.get("t")
        key = f"law.{code}"
        text = t(key) if t is not None else code
        return code if text == key else text

    env.filters["law"] = law
    from .sms import is_landline

    env.filters["landline"] = is_landline                 # {{ p.phone | landline }} → Festnetz (keine SMS)
    from ..knowledge import anatomy, figures

    env.globals.update(
        BODYMAP=figures.BODYMAP,
        SKELETON=figures.SKELETON,
        anat=anatomy,
        static=lambda path: f"/static/{path}?v={app.get(STATIC_VERSION, '0')}",
        icon=icon,
        fmt_time=timeutil.fmt_time,
        format_ahv=_format_ahv,
        LANG_NAMES=LANG_NAMES,
        STAFF_LANGS=STAFF_LANGS,
        PATIENT_LANGS=PATIENT_LANGS,
    )
    return env


def _nav_for(request: web.Request) -> list[dict[str, Any]]:
    """Menü des aktuellen Bildschirms bzw. der Patienten-App (nur Module, die für die Praxis eingeschaltet sind)."""
    user = request.get("user")
    screen = request.get("screen")
    if screen is not None and user is not None:
        key, base, practice_id = screen.key, screen.prefix, user["practice_id"]
    elif request.get("patient") is not None:
        key, base, practice_id = "patient", "/p", request["patient_session"]["practice_id"]
        user = request["patient"]
    else:
        return []
    db = request.app[DB]
    badges = request.app[BADGES]
    entries = [e for e in request.app.get(NAV, []) if e.screen == key
               and module_enabled(db, practice_id, e.module, request.app[MODULES])]
    out = []
    for e in sorted(entries, key=lambda e: e.item.order):
        item = e.item
        href = base + item.path
        count = 0
        if item.badge and item.badge in badges:
            try:
                count = badges[item.badge](db, practice_id, user)
            except Exception:  # Zähler darf die Seite nie kaputt machen
                log.exception("Badge %s fehlgeschlagen", item.badge)
        out.append({"label_key": item.label_key, "short_key": item.short_key or item.label_key, "icon": item.icon, "href": href,
                    "badge": count, "phone": item.phone,
                    "rail": item.rail, "module": e.module,
                    "active": request.path == href or request.path.startswith(href.rstrip("/") + "/")})
    # Nur der genaueste Eintrag ist aktiv (z. B. «Warteliste» /agenda/warteliste, nicht zusätzlich «Agenda» /agenda)
    best = max((len(o["href"]) for o in out if o["active"]), default=0)
    for o in out:
        o["active"] = o["active"] and len(o["href"]) == best
    return out


def localized(row, field: str, lang: str) -> str:
    """Spalte field_<lang> lesen, sonst field_de (z. B. Übungsnamen)."""
    if row is None:
        return ""
    keys = row.keys() if hasattr(row, "keys") else []
    for code in (lang, "de", "fr"):
        key = f"{field}_{code}"
        if key in keys and row[key]:
            return row[key]
    return ""


def _nsystem_banners(request: web.Request, lang: str) -> tuple[list[dict], dict | None]:
    """Wartungshinweise von NSystem (nur Personal, im Zeitfenster, nicht ausgeblendet) und – für die Leitung – offener Supportzugang."""
    from . import nsystem

    screen, user = request.get("screen"), request.get("user")
    if screen is None or user is None:
        return [], None
    try:
        notices = nsystem.active_notices(request.app[DB], user, lang)
        support = nsystem.support_status(request.app[SETTINGS]) if screen.key == "leitung" else None
    except Exception:  # ein Hinweis darf keine Seite kaputt machen
        log.exception("NSystem-Hinweise")
        return [], None
    return notices, support if support and support["open"] else None


def base_context(request: web.Request) -> dict[str, Any]:
    app = request.app
    lang = request.get("lang", "de")
    i18n = app[I18N]
    flash_key = request.cookies.get(FLASH_COOKIE)
    if flash_key:
        request["_flash_read"] = True
    user = request.get("user")
    practice = None
    if user is not None and user["practice_id"]:
        practice = app[DB].one("SELECT * FROM practices WHERE id = ?", (user["practice_id"],))
    elif request.get("patient_session") is not None:
        practice = app[DB].one("SELECT * FROM practices WHERE id = ?", (request["patient_session"]["practice_id"],))

    def t(key: str, **kwargs) -> str:
        return i18n.t(lang, key, **kwargs)

    def module_on(name: str) -> bool:
        return module_enabled(app[DB], practice["id"] if practice else None, name, app[MODULES])

    notices, support_open = _nsystem_banners(request, lang)
    return {
        "nsystem_notices": notices,
        "nsystem_support": support_open,
        "request": request,
        "lang": lang,
        "dir": text_dir(lang),
        "t": t,
        "user": user,
        "patient_user": request.get("patient"),
        "practice": practice,
        "csrf": request.get("csrf", ""),
        "nav": _nav_for(request),
        "screen": request.get("screen"),
        "S": screen_prefix(request),
        "my_screens": screens_for(user) if user is not None else [],
        "flash": i18n.t(lang, flash_key) if flash_key else None,
        "demo": app[SETTINGS].demo,
        # Nur Vorführung (NFIZYO_AI_MODE=cloud_demo): Host des externen KI-Dienstes, sonst None
        "ai_cloud": getattr(app[AI], "cloud_host", None) if AI in app else None,
        # KI-Server Schweiz statt KI auf dem Praxis-Server: Hinweise zum Datenweg müssen das sagen
        "ai_remote": AI in app and getattr(app[AI], "mode", "local") == "remote",
        # Lokale KI gehört zum Produkt: fehlt ein Baustein, sieht es Leitung und Therapie sofort
        "ai_missing": ", ".join(i18n.t(lang, f"settings.ai.{k}") for k in app[AI].missing())
        if (request.get("screen") is not None and request["screen"].key in ("therapie", "leitung")
            and not app[SETTINGS].demo and AI in app) else "",
        "path": request.path,
        "module_on": module_on,
        "fmt_date": lambda v, weekday=False, year=True: timeutil.fmt_date(v, lang, weekday=weekday, year=year),
        "date_range": lambda a, b, weekday=False: formats.date_range(a, b, lang, weekday=weekday),
        "pct": lambda v, digits=1: formats.percent(v, lang, digits),
        "dec": lambda v, digits=1: formats.decimal(v, lang, digits),
        "fmt_long_date": lambda d: timeutil.fmt_long_date(formats.as_date(d), lang) if formats.as_date(d) else "",
        "localized": lambda row, field: localized(row, field, lang),
    }


async def render(request: web.Request, template: str, ctx: dict[str, Any] | None = None, status: int = 200) -> web.Response:
    context = base_context(request)
    if ctx:
        context.update(ctx)
    return aiohttp_jinja2.render_template(template, request, context, status=status)
