"""NSystem-Übersicht: Web-Oberfläche für NSystem-Personal + Schnittstelle für die Meldungen der Boxen."""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp_jinja2
import jinja2
from aiohttp import web

from ..agent import signing
from ..core import crypto, patientdata, totp
from ..core.auth import LoginThrottle
from ..core.box import ACTIONS, valid_args
from ..core.db import Database, utcnow
from ..core.security import hash_password, new_token, token_hash, verify_password
from ..core.timeutil import TZ
from ..core.web import client_ip, icon
from . import health, ops, releases
from .config import CentralSettings

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
STATIC = HERE.parent / "web" / "static"
CFG = web.AppKey("central_settings", object)
DBK = web.AppKey("central_db", object)
THROTTLE = web.AppKey("central_throttle", object)
BG = web.AppKey("central_bg", asyncio.Task)
COOKIE = "nfc_s"
SESSION_HOURS = 12
MAX_REPORT = 256 * 1024
PURPOSE_BOX = "box-key"
PURPOSE_TOTP = "central-totp"


# --------------------------------------------------------------------------- Datenbank-Hilfen
def audit(db: Database, staff_id: int | None, action: str, box_id: str | None = None, detail: dict | None = None) -> None:
    db.execute("INSERT INTO audit (staff_id, action, box_id, detail, at) VALUES (?,?,?,?,?)",
               (staff_id, action, box_id, json.dumps(detail, ensure_ascii=False) if detail else None, utcnow()))


def add_box(db: Database, secret_key: bytes, name: str) -> tuple[str, str]:
    """Neue Box: (Box-ID, Gerätegeheimnis). Das Geheimnis wird nur verschlüsselt gespeichert."""
    box_id = "bx_" + new_token(9).replace("-", "x").replace("_", "y")
    key = new_token(32)
    db.execute("INSERT INTO boxes (id, name, key_enc, created_at) VALUES (?,?,?,?)",
               (box_id, name.strip()[:120], crypto.encrypt(secret_key, PURPOSE_BOX, key), utcnow()))
    audit(db, None, "box_created", box_id, {"name": name})
    return box_id, key


def add_staff(db: Database, email: str, name: str, password: str) -> int:
    if len(password) < 12:
        raise ValueError("Passwort zu kurz (mindestens 12 Zeichen).")
    sid = db.execute("INSERT INTO staff (email, name, password_hash, created_at) VALUES (?,?,?,?)",
                     (email.strip().lower(), name.strip(), hash_password(password), utcnow()))
    audit(db, None, "staff_created", None, {"email": email})
    return sid


def signing_key(settings: CentralSettings) -> bytes:
    return settings.signing_key_path.read_bytes()


def latest_report(db: Database, box_id: str) -> dict | None:
    row = db.one("SELECT payload FROM reports WHERE box_id = ? ORDER BY id DESC LIMIT 1", (box_id,))
    return json.loads(row["payload"]) if row else None


# --------------------------------------------------------------------------- Push (ntfy)
async def push(settings: CentralSettings, title: str, message: str, priority: str = "high") -> bool:
    """Nachricht aufs Telefon (ntfy). Enthält nur Box-Name und Problem/Bereich – keine Patientendaten, keine Anfragetexte."""
    if not settings.ntfy_url:
        return False
    import aiohttp

    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": priority,
               "Tags": "warning" if priority in ("high", "urgent") else "information_source"}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(settings.ntfy_url, data=message.encode("utf-8"), headers=headers) as resp:
                return resp.status < 300
    except Exception:  # noqa: BLE001
        log.warning("ntfy nicht erreichbar")
        return False


async def update_alerts(app: web.Application, box, found: list[health.Issue]) -> None:
    db = app[DBK]
    open_rows = {r["kind"]: r for r in db.query("SELECT * FROM alerts WHERE box_id = ? AND closed_at IS NULL", (box["id"],))}
    kinds = {i.kind for i in found}
    for kind, row in open_rows.items():
        if kind not in kinds and kind != "silent":
            db.execute("UPDATE alerts SET closed_at = ? WHERE id = ?", (utcnow(), row["id"]))
    for issue in found:
        if issue.kind in open_rows:
            if open_rows[issue.kind]["message"] != issue.message:   # gleiche Warnung, neuer Stand (z. B. Speicher %, Uhr ±s)
                db.execute("UPDATE alerts SET message = ? WHERE id = ?", (issue.message, open_rows[issue.kind]["id"]))
            continue
        aid = db.execute("INSERT INTO alerts (box_id, kind, message, opened_at) VALUES (?,?,?,?)",
                         (box["id"], issue.kind, issue.message, utcnow()))
        if issue.push and await push(app[CFG], f"N-Fizyo: {box['name']}", issue.message):
            db.execute("UPDATE alerts SET pushed = 1 WHERE id = ?", (aid,))


async def watch(app: web.Application) -> None:
    """Jede Minute: stumme Boxen melden, abgelaufene Befehle schliessen, alte Meldungen löschen (7 Tage)."""
    while True:
        try:
            db = app[DBK]
            cfg = app[CFG]
            limit = (datetime.now(timezone.utc) - timedelta(minutes=cfg.silence_minutes)).replace(microsecond=0).isoformat()
            for box in db.query("SELECT * FROM boxes WHERE archived = 0 AND last_report_at IS NOT NULL"):
                silent = box["last_report_at"] < limit
                open_row = db.one("SELECT id FROM alerts WHERE box_id = ? AND kind = 'silent' AND closed_at IS NULL", (box["id"],))
                if silent and open_row is None:
                    msg = f"Keine Meldung seit {cfg.silence_minutes} Minuten (Strom, Internet, Box?)"
                    aid = db.execute("INSERT INTO alerts (box_id, kind, message, opened_at) VALUES (?,?,?,?)",
                                     (box["id"], "silent", msg, utcnow()))
                    if await push(cfg, f"N-Fizyo: {box['name']}", msg, "urgent"):
                        db.execute("UPDATE alerts SET pushed = 1 WHERE id = ?", (aid,))
                elif not silent and open_row is not None:
                    db.execute("UPDATE alerts SET closed_at = ? WHERE id = ?", (utcnow(), open_row["id"]))
            db.execute("UPDATE commands SET status = 'expired' WHERE status = 'pending' AND expires_at < ?", (time.time(),))
            await check_own_backup(app)
            old = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0).isoformat()
            db.execute("DELETE FROM reports WHERE received_at < ? AND id NOT IN (SELECT MAX(id) FROM reports GROUP BY box_id)", (old,))
        except Exception:  # pragma: no cover
            log.exception("Überwachung")
        await asyncio.sleep(60)


async def check_own_backup(app: web.Application) -> None:
    """Sicherung des Panels selbst: bei Problem einmal Push (bis es wieder gut ist)."""
    db, cfg = app[DBK], app[CFG]
    st = ops.backup_state(cfg)
    if not st["known"]:
        return
    last = ops.get_state(db, "backup_alert")
    if st["problem"] and st["problem"] != last:
        await push(cfg, "N-Fizyo: NSystem-Übersicht", st["problem"])
        ops.set_state(db, "backup_alert", st["problem"])
    elif not st["problem"] and last:
        ops.set_state(db, "backup_alert", None)


def _local(ts) -> str:
    """Unix-Zeit oder ISO-Zeit → «11.11.2026 14:30» (Zürich)."""
    if not ts:
        return "–"
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return ts[:16]
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    return datetime.fromtimestamp(float(ts), TZ).strftime("%d.%m.%Y %H:%M")


async def _after_report(app: web.Application, box, report: dict) -> None:
    """Protokoll und Support-Anfragen aus der Meldung übernehmen; Push für neue Anfragen und geöffneten Supportzugang."""
    db, cfg = app[DBK], app[CFG]
    for e in ops.ingest_events(db, box["id"], report.get("events")):
        if e["kind"] == "support_opened":
            await push(cfg, f"N-Fizyo: {box['name']}", f"Supportzugang von der Praxis geöffnet bis {_local(e['detail'].get('until'))}",
                       "default")
    for r in ops.ingest_requests(db, box["id"], report.get("support_requests")):
        text = f"Neue Support-Anfrage: {ops.AREAS[r['area']]} ({ops.URGENCIES[r['urgency']]})"
        if r["with_access"]:
            text += " · Supportzugang geöffnet"
        if r["blocked"]:
            text += " · Text blockiert (Patientendaten?)"
        await push(cfg, f"N-Fizyo: {box['name']}", text, "urgent" if r["urgency"] == "urgent" else "default")


# --------------------------------------------------------------------------- Schnittstelle für die Boxen
async def api_report(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    cfg = request.app[CFG]
    box = db.one("SELECT * FROM boxes WHERE id = ? AND archived = 0", (request.headers.get("X-Box-Id", ""),))
    body = await request.content.read(MAX_REPORT + 1)
    if box is None or len(body) > MAX_REPORT:
        raise web.HTTPForbidden()
    key = crypto.decrypt(cfg.secret_key, PURPOSE_BOX, box["key_enc"])
    ts = request.headers.get("X-Timestamp", "")
    if not signing.check_report(key, ts, body, request.headers.get("X-Signature", "")):
        audit(db, None, "report_rejected", box["id"])
        if ts.isdigit() and hmac.compare_digest(signing.report_signature(key, ts, body), request.headers.get("X-Signature", "")):
            # Echte Meldung, nur die Uhr der Box geht falsch: nicht als «stumm» erscheinen lassen, sondern genau das melden
            off = int(ts) - int(time.time())
            await update_alerts(request.app, box, [health.Issue("clock", health.clock_message(off))])
            return web.json_response({"error": "clock", "server_time": int(time.time())}, status=403)
        raise web.HTTPForbidden()
    try:
        report = json.loads(body)
    except json.JSONDecodeError:
        raise web.HTTPBadRequest()
    if not isinstance(report, dict):
        raise web.HTTPBadRequest()
    report["clock_offset_s"] = int(ts) - int(time.time())   # Uhr der Box gegenüber NSystem (Anmeldecodes, Rechnungsdaten)
    now = utcnow()
    # Gespeichert wird die Meldung OHNE Texte der Support-Anfragen (die landen geprüft in support_requests – oder gar nicht)
    stored = dict(report, support_requests=[{"uid": str(r.get("uid"))[:80]} for r in report.get("support_requests") or []
                                             if isinstance(r, dict)])
    db.execute("INSERT INTO reports (box_id, received_at, payload) VALUES (?,?,?)",
               (box["id"], now, json.dumps(stored, ensure_ascii=False)))
    db.execute("UPDATE boxes SET last_report_at = ?, last_version = ? WHERE id = ?", (now, str(report.get("app_version"))[:20], box["id"]))
    for c in report.get("commands") or []:
        st = c.get("status") if c.get("status") in ("running", "done", "failed", "rejected") else None
        if st:
            db.execute("UPDATE commands SET status = ?, result = ?, finished_at = ? WHERE id = ? AND box_id = ?",
                       (st, str(c.get("result") or c.get("reason") or "")[:4000], c.get("finished_at"), str(c.get("id")), box["id"]))
    await update_alerts(request.app, box, health.issues(report))
    await _after_report(request.app, box, report)
    pending = db.query("SELECT * FROM commands WHERE box_id = ? AND status = 'pending' AND expires_at > ?"
                       " AND (deliver_after IS NULL OR deliver_after <= ?)", (box["id"], time.time(), time.time()))
    out = []
    for c in pending:
        out.append({"id": c["id"], "box_id": c["box_id"], "action": c["action"], "args": json.loads(c["args"]), "issued_by": c["issued_by"],
                    "issued_at": c["issued_at"], "expires_at": c["expires_at"], "signature": c["signature"]})
        db.execute("UPDATE commands SET status = 'delivered', delivered_at = ? WHERE id = ?", (now, c["id"]))
    pem = signing_key(cfg)
    return web.json_response({"ok": True, "commands": out, "notices": ops.notices_for(db, box["id"], pem),
                              "support_replies": ops.replies_for(db, box["id"], report.get("support_known"), pem)})


async def health_check(request: web.Request) -> web.StreamResponse:
    request.app[DBK].value("SELECT 1")
    return web.json_response({"status": "ok"})


# --------------------------------------------------------------------------- Anmeldung (immer mit Zwei-Schritt)
@web.middleware
async def session_mw(request: web.Request, handler):
    db = request.app[DBK]
    request["staff"] = None
    request["pending"] = None
    token = request.cookies.get(COOKIE)
    sess = db.one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash(token),)) if token else None
    if sess is not None and sess["expires_at"] < utcnow():
        sess = None
    if sess is not None:
        staff = db.one("SELECT * FROM staff WHERE id = ? AND active = 1", (sess["staff_id"],))
        if staff is None:
            sess = None
        elif sess["stage"] == "ok":
            request["staff"] = staff
        else:
            request["pending"] = staff
    request["session"] = sess
    csrf = sess["csrf"] if sess else request.cookies.get("nfc_csrf") or new_token(16)
    request["csrf"] = csrf
    if request.method == "POST" and not request.path.startswith("/api/"):
        form = await request.post()
        if not hmac.compare_digest(str(form.get("_csrf") or ""), csrf):
            raise web.HTTPForbidden(text="CSRF")
    resp = await handler(request)
    if sess is None and not request.cookies.get("nfc_csrf"):
        resp.set_cookie("nfc_csrf", csrf, httponly=True, samesite="Strict", secure=request.app[CFG].secure_cookies)
    return resp


# Gleiche Schutz-Kopfzeilen wie die Praxis-App (core/web.py): nur eigene Skripte/Stile/Bilder, keine Rahmen, keine fremden
# Formularziele; HSTS bei https. Für JEDE Antwort (auch Fehler, Weiterleitungen, statische Dateien) über on_response_prepare.
PANEL_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
             "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")


async def security_headers(request: web.Request, resp: web.StreamResponse) -> None:
    resp.headers.setdefault("Content-Security-Policy", PANEL_CSP)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.app[CFG].secure_cookies:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


def staff_only(handler):
    @functools.wraps(handler)
    async def wrapper(request):
        if request.get("staff") is None:
            raise web.HTTPFound("/login")
        return await handler(request)

    return wrapper


def _new_session(request: web.Request, staff_id: int, stage: str) -> web.HTTPFound:
    db = request.app[DBK]
    if request.get("session"):
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (request["session"]["token_hash"],))
    token, csrf = new_token(), new_token(16)
    hours = SESSION_HOURS if stage == "ok" else 0.2
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
    db.execute("INSERT INTO sessions (token_hash, staff_id, stage, csrf, created_at, expires_at) VALUES (?,?,?,?,?,?)",
               (token_hash(token), staff_id, stage, csrf, utcnow(), expires))
    target = {"ok": "/", "code": "/login/code", "setup": "/login/einrichten"}[stage]
    resp = web.HTTPFound(target)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", secure=request.app[CFG].secure_cookies)
    return resp


async def render(request, template: str, ctx: dict | None = None, status: int = 200):
    new_requests = request.app[DBK].value("SELECT COUNT(*) FROM support_requests WHERE status = 'new'", (), 0) if request.get("staff") else 0
    base = {"staff": request.get("staff"), "csrf": request["csrf"], "icon": icon, "path": request.path, "new_requests": new_requests,
            "local": _local, "areas": ops.AREAS, "urgencies": ops.URGENCIES, "statuses": ops.STATUSES}
    return aiohttp_jinja2.render_template(f"central/{template}", request, {**base, **(ctx or {})}, status=status)


async def login(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    error = None
    if request.method == "POST":
        form = await request.post()
        email = str(form.get("email") or "").strip().lower()
        key = f"{client_ip(request, request.app[CFG].trusted_proxies)}:{email}"
        if request.app[THROTTLE].blocked(key):
            error = "Zu viele Versuche. Bitte später erneut."
        else:
            staff = db.one("SELECT * FROM staff WHERE email = ? AND active = 1", (email,))
            if staff and await asyncio.to_thread(verify_password, str(form.get("password") or "")[:1024], staff["password_hash"]):
                request.app[THROTTLE].reset(key)
                audit(db, staff["id"], "password_ok")
                raise _new_session(request, staff["id"], "code" if staff["totp_enabled_at"] else "setup")
            request.app[THROTTLE].fail(key)
            error = "E-Mail oder Passwort stimmt nicht."
    return await render(request, "login.html", {"error": error})


async def login_code(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    cfg = request.app[CFG]
    staff = request.get("pending")
    if staff is None or request["session"]["stage"] != "code":
        raise web.HTTPFound("/login")
    error = None
    if request.method == "POST":
        form = await request.post()
        key = f"code:{staff['id']}"
        if request.app[THROTTLE].blocked(key):
            error = "Zu viele Versuche."
        elif _check_code(db, cfg, staff, str(form.get("code") or "")):
            request.app[THROTTLE].reset(key)
            audit(db, staff["id"], "login")
            raise _new_session(request, staff["id"], "ok")
        else:
            request.app[THROTTLE].fail(key)
            error = "Code stimmt nicht."
    return await render(request, "code.html", {"error": error})


def _check_code(db, cfg, staff, code: str) -> bool:
    if totp.looks_like_recovery(code):
        left = totp.use_recovery(json.loads(staff["recovery_codes"] or "[]"), code)
        if left is None:
            return False
        db.execute("UPDATE staff SET recovery_codes = ? WHERE id = ?", (json.dumps(left), staff["id"]))
        audit(db, staff["id"], "recovery_code_used")
        return True
    secret = crypto.decrypt(cfg.secret_key, PURPOSE_TOTP, staff["totp_secret"])
    step = totp.verify(secret, code, staff["totp_last_step"])
    if step is None:
        return False
    db.execute("UPDATE staff SET totp_last_step = ? WHERE id = ?", (step, staff["id"]))
    return True


async def login_setup(request: web.Request) -> web.StreamResponse:
    import segno

    db = request.app[DBK]
    cfg = request.app[CFG]
    staff = request.get("pending")
    if staff is None or request["session"]["stage"] != "setup":
        raise web.HTTPFound("/login")
    error = None
    if request.method == "POST" and staff["totp_pending"]:
        form = await request.post()
        secret = crypto.decrypt(cfg.secret_key, PURPOSE_TOTP, staff["totp_pending"])
        step = totp.verify(secret, str(form.get("code") or ""))
        if step is not None:
            codes = totp.new_recovery_codes()
            db.execute("UPDATE staff SET totp_secret = totp_pending, totp_pending = NULL, totp_enabled_at = ?, totp_last_step = ?,"
                       " recovery_codes = ? WHERE id = ?", (utcnow(), step, json.dumps([totp.hash_recovery(c) for c in codes]), staff["id"]))
            audit(db, staff["id"], "mfa_enabled")
            resp = await render(request, "codes.html", {"codes": codes})
            login = _new_session(request, staff["id"], "ok")
            for c in login.cookies.values():
                resp.cookies[c.key] = c
            return resp
        error = "Code stimmt nicht."
    secret = crypto.decrypt(cfg.secret_key, PURPOSE_TOTP, staff["totp_pending"]) if staff["totp_pending"] else None
    if not secret:
        secret = totp.new_secret()
        db.execute("UPDATE staff SET totp_pending = ? WHERE id = ?", (crypto.encrypt(cfg.secret_key, PURPOSE_TOTP, secret), staff["id"]))
    qr = segno.make(totp.otpauth_uri(secret, staff["email"], "NSystem Übersicht"), error="m").svg_inline(scale=5, border=2)
    return await render(request, "setup.html", {"qr": qr, "secret": totp.pretty_secret(secret), "error": error})


async def logout(request: web.Request) -> web.StreamResponse:
    if request.get("session"):
        request.app[DBK].execute("DELETE FROM sessions WHERE token_hash = ?", (request["session"]["token_hash"],))
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    raise resp


# --------------------------------------------------------------------------- Seiten
def _age_min(iso: str | None) -> int | None:
    if not iso:
        return None
    return int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() // 60)


@staff_only
async def dashboard(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    rows = []
    for b in db.query("SELECT * FROM boxes WHERE archived = 0 ORDER BY name"):
        rep = latest_report(db, b["id"]) or {}
        alerts = db.query("SELECT kind, message FROM alerts WHERE box_id = ? AND closed_at IS NULL", (b["id"],))
        check = health.checklist(rep or None)
        rows.append({"box": b, "age": _age_min(b["last_report_at"]), "rep": rep, "alerts": alerts, "support": ops.support_state(rep),
                     "check_done": sum(1 for c in check if c["ok"] or not c["required"]), "check_total": len(check),
                     "state": "error" if any(a["kind"] in ("silent", "backup", "disk", "ai", "runner", "jobs_stuck") for a in alerts)
                     else ("warn" if alerts or not b["last_report_at"] else "ok")})
    return await render(request, "dashboard.html", {"rows": rows, "backup": ops.backup_state(request.app[CFG])})


@staff_only
async def box_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    box = db.one("SELECT * FROM boxes WHERE id = ?", (request.match_info["bid"],))
    if box is None:
        raise web.HTTPNotFound()
    rep = latest_report(db, box["id"])
    cfg = request.app[CFG]
    rels, rel_error = await asyncio.to_thread(releases.available, cfg)
    events = []
    for e in db.query("SELECT * FROM box_events WHERE box_id = ? ORDER BY at DESC, id DESC LIMIT 40", (box["id"],)):
        events.append({**dict(e), "data": json.loads(e["detail"]) if e["detail"] else {}})
    return await render(request, "box.html", {
        "box": box, "rep": rep or {}, "age": _age_min(box["last_report_at"]), "check": health.checklist(rep),
        "alerts": db.query("SELECT * FROM alerts WHERE box_id = ? ORDER BY closed_at IS NOT NULL, id DESC LIMIT 30", (box["id"],)),
        "commands": db.query("SELECT * FROM commands WHERE box_id = ? ORDER BY issued_at DESC LIMIT 30", (box["id"],)),
        "actions": ACTIONS, "flash": request.query.get("ok"), "releases": rels[:15], "release_error": rel_error,
        "support": ops.support_state(rep), "events": events, "event_labels": ops.EVENT_LABELS, "close_reasons": ops.CLOSE_REASONS,
        "requests": db.query("SELECT * FROM support_requests WHERE box_id = ? ORDER BY received_at DESC LIMIT 10", (box["id"],)),
        "notices": db.query("SELECT * FROM notices n WHERE n.withdrawn_at IS NULL AND n.ends_at > ? AND (n.all_boxes = 1 OR EXISTS"
                            " (SELECT 1 FROM notice_boxes nb WHERE nb.notice_id = n.id AND nb.box_id = ?)) ORDER BY starts_at",
                            (utcnow(), box["id"])),
        "ssh_host": cfg.support_ssh_host, "now_ts": time.time()})


@staff_only
async def command_create(request: web.Request) -> web.StreamResponse:
    """Signierten Befehl ausstellen. Zur Bestätigung immer der aktuelle Code aus der App (zweite Person nicht nötig).

    «Neue Version»: Version aus dem signierten Release-Index wählen → Datei und SHA-256 werden eingetragen. Optional zu einer
    Uhrzeit (die Box bekommt den Befehl erst dann) und mit automatischem Wartungshinweis an die Praxis."""
    db = request.app[DBK]
    cfg = request.app[CFG]
    staff = request["staff"]
    box = db.one("SELECT * FROM boxes WHERE id = ? AND archived = 0", (request.match_info["bid"],))
    if box is None:
        raise web.HTTPNotFound()
    form = await request.post()
    action = str(form.get("action") or "")
    back = f"/box/{box['id']}"
    args: dict = {}
    manifest = None
    if action == "update":
        version = str(form.get("release") or "").strip()
        if version:
            manifest = await asyncio.to_thread(releases.find, cfg, version)
            if manifest is None:
                raise web.HTTPFound(back + "?ok=release")
            args = {"file": manifest["file"], "sha256": manifest["sha256"], "version": manifest["version"]}
        else:
            args = {"file": str(form.get("file") or "").strip(), "sha256": str(form.get("sha256") or "").strip().lower()}
    if not valid_args(action, args):
        raise web.HTTPFound(back + "?ok=invalid")
    try:
        when = ops.schedule_time(str(form.get("when") or ""), TZ) if action == "update" else None
    except ValueError:
        raise web.HTTPFound(back + "?ok=invalid")
    if when is not None and when.timestamp() < time.time() - 60:
        raise web.HTTPFound(back + "?ok=past")
    fresh = db.one("SELECT * FROM staff WHERE id = ?", (staff["id"],))
    if not _check_code(db, cfg, fresh, str(form.get("code") or "")):
        raise web.HTTPFound(back + "?ok=code")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = when.timestamp() if when else time.time()
    cmd = {"id": str(uuid.uuid4()), "box_id": box["id"], "action": action, "args": args, "issued_by": staff["name"],
           "issued_at": now.isoformat(), "expires_at": start + cfg.command_minutes * 60}
    cmd["signature"] = signing.sign_command(signing_key(cfg), cmd)
    notice_id = None
    if when is not None and form.get("notice") and when.timestamp() > time.time() + 300:
        de, fr = ops.update_notice_texts(args.get("version") or args["file"], when.astimezone(TZ))
        notice_id = ops.create_notice(db, text_de=de, text_fr=fr, starts_at=now, ends_at=when + timedelta(minutes=45),
                                      boxes=[box["id"]], staff_id=staff["id"])
    db.execute("INSERT INTO commands (id, box_id, action, args, issued_by, staff_id, issued_at, expires_at, signature, deliver_after, notice_id)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (cmd["id"], box["id"], action, json.dumps(args), staff["name"], staff["id"], cmd["issued_at"], cmd["expires_at"],
                cmd["signature"], when.timestamp() if when else None, notice_id))
    audit(db, staff["id"], "command", box["id"], {"action": action, "args": args, "id": cmd["id"],
                                                  **({"deliver_after": when.isoformat()} if when else {}),
                                                  **({"notice": notice_id} if notice_id else {})})
    raise web.HTTPFound(back + ("?ok=scheduled" if when else "?ok=command"))


@staff_only
async def command_cancel(request: web.Request) -> web.StreamResponse:
    """Geplanten (noch nicht abgeholten) Befehl abbrechen; ein automatischer Wartungshinweis wird zurückgezogen."""
    db = request.app[DBK]
    staff = request["staff"]
    row = db.one("SELECT * FROM commands WHERE id = ? AND box_id = ? AND status = 'pending'", (request.match_info["cid"], request.match_info["bid"]))
    if row is not None:
        db.execute("UPDATE commands SET status = 'expired', result = ? WHERE id = ?", (f"Abgebrochen von {staff['name']}", row["id"]))
        if row["notice_id"]:
            db.execute("UPDATE notices SET withdrawn_at = ? WHERE id = ?", (utcnow(), row["notice_id"]))
        audit(db, staff["id"], "command_cancelled", row["box_id"], {"id": row["id"]})
    raise web.HTTPFound(f"/box/{request.match_info['bid']}?ok=cancelled")


# --------------------------------------------------------------------------- Support-Anfragen (Eingang)
@staff_only
async def support_inbox(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    status = request.query.get("status", "open")
    where = {"open": "r.status != 'done'", "new": "r.status = 'new'", "done": "r.status = 'done'", "all": "1 = 1"}.get(status, "r.status != 'done'")
    rows = db.query(f"SELECT r.*, b.name AS box_name FROM support_requests r JOIN boxes b ON b.id = r.box_id WHERE {where}"  # noqa: S608
                    " ORDER BY r.status = 'done', CASE r.urgency WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, r.received_at DESC LIMIT 300")
    return await render(request, "support.html", {"rows": rows, "filter": status})


@staff_only
async def support_detail(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    staff = request["staff"]
    rid = int(request.match_info["rid"]) if request.match_info["rid"].isdigit() else 0
    row = db.one("SELECT r.*, b.name AS box_name FROM support_requests r JOIN boxes b ON b.id = r.box_id WHERE r.id = ?", (rid,))
    if row is None:
        raise web.HTTPNotFound()
    error = None
    if request.method == "POST":
        form = await request.post()
        reply = str(form.get("reply") or "")
        found = patientdata.scan(reply)
        if found:
            error = "Die Antwort enthält vermutlich persönliche Daten (" + ", ".join(found) + ") – bitte entfernen."
        elif ops.answer(db, rid, status=str(form.get("status") or ""), reply=reply, staff_name=staff["name"]):
            audit(db, staff["id"], "support_reply", row["box_id"], {"request": row["uid"], "status": str(form.get("status"))})
            raise web.HTTPFound(f"/support/{rid}?ok=1")
    return await render(request, "support_detail.html", {"r": row, "error": error, "flash": request.query.get("ok"),
                                                         "box_support": ops.support_state(latest_report(db, row["box_id"]))})


# --------------------------------------------------------------------------- Wartungshinweise
@staff_only
async def notices_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    staff = request["staff"]
    error = None
    if request.method == "POST":
        form = await request.post()
        try:
            start = ops.schedule_time(str(form.get("starts_at") or ""), TZ) or datetime.now(timezone.utc)
            end = ops.schedule_time(str(form.get("ends_at") or ""), TZ)
            if end is None:
                raise ValueError("Ende fehlt")
            boxes = [str(b) for b in form.getall("box", [])] if form.get("target") == "selected" else None
            if form.get("target") == "selected" and not boxes:
                raise ValueError("keine Box gewählt")
            nid = ops.create_notice(db, text_de=str(form.get("text_de") or ""), text_fr=str(form.get("text_fr") or ""),
                                    starts_at=start, ends_at=end, boxes=boxes, staff_id=staff["id"])
            audit(db, staff["id"], "notice_created", None, {"id": nid, "boxes": boxes or "alle"})
            raise web.HTTPFound("/hinweise?ok=1")
        except ValueError as exc:
            error = str(exc)
    rows = db.query("SELECT n.*, s.name AS staff_name, (SELECT group_concat(b.name, ', ') FROM notice_boxes nb JOIN boxes b ON b.id = nb.box_id"
                    " WHERE nb.notice_id = n.id) AS box_names FROM notices n LEFT JOIN staff s ON s.id = n.created_by"
                    " ORDER BY n.withdrawn_at IS NOT NULL, n.ends_at < ?, n.starts_at DESC LIMIT 100", (utcnow(),))
    return await render(request, "notices.html", {"rows": rows, "error": error, "flash": request.query.get("ok"), "now": utcnow(),
                                                  "boxes": db.query("SELECT id, name FROM boxes WHERE archived = 0 ORDER BY name")})


@staff_only
async def notice_withdraw(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    db.execute("UPDATE notices SET withdrawn_at = ? WHERE id = ? AND withdrawn_at IS NULL", (utcnow(), request.match_info["nid"]))
    audit(db, request["staff"]["id"], "notice_withdrawn", None, {"id": request.match_info["nid"]})
    raise web.HTTPFound("/hinweise")


@staff_only
async def alerts_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DBK]
    rows = db.query("SELECT a.*, b.name AS box_name FROM alerts a JOIN boxes b ON b.id = a.box_id"
                    " ORDER BY a.closed_at IS NOT NULL, a.id DESC LIMIT 200")
    return await render(request, "alerts.html", {"alerts": rows})


@staff_only
async def audit_page(request: web.Request) -> web.StreamResponse:
    rows = request.app[DBK].query("SELECT a.*, s.name AS staff_name FROM audit a LEFT JOIN staff s ON s.id = a.staff_id ORDER BY a.id DESC LIMIT 300")
    return await render(request, "audit.html", {"rows": rows})


def create_central_app(settings: CentralSettings, *, start_watch: bool = True) -> web.Application:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = web.Application(middlewares=[session_mw], client_max_size=MAX_REPORT * 2)
    app[CFG] = settings
    app.on_response_prepare.append(security_headers)
    db = Database(settings.db_path)
    db.migrate([("central", HERE / "migrations")])
    app[DBK] = db
    app[THROTTLE] = LoginThrottle()
    env = aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(HERE / "templates")),
                               autoescape=jinja2.select_autoescape(["html"]))
    env.filters["tojson_pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2)
    env.globals["static"] = lambda p: f"/static/{p}"
    env.globals["quote"] = quote
    r = app.router
    r.add_get("/health", health_check)
    r.add_post("/api/v1/report", api_report)
    r.add_get("/login", login)
    r.add_post("/login", login)
    r.add_get("/login/code", login_code)
    r.add_post("/login/code", login_code)
    r.add_get("/login/einrichten", login_setup)
    r.add_post("/login/einrichten", login_setup)
    r.add_post("/logout", logout)
    r.add_get("/", dashboard)
    r.add_get("/box/{bid}", box_page)
    r.add_post("/box/{bid}/befehl", command_create)
    r.add_post("/box/{bid}/befehl/{cid}/abbrechen", command_cancel)
    r.add_get("/support", support_inbox)
    r.add_get("/support/{rid}", support_detail)
    r.add_post("/support/{rid}", support_detail)
    r.add_get("/hinweise", notices_page)
    r.add_post("/hinweise", notices_page)
    r.add_post("/hinweise/{nid}/zurueckziehen", notice_withdraw)
    r.add_get("/warnungen", alerts_page)
    r.add_get("/protokoll", audit_page)
    r.add_static("/static", STATIC)

    async def start(app):
        if start_watch:
            app[BG] = asyncio.create_task(watch(app))

    async def stop(app):
        if BG in app:
            app[BG].cancel()
        db.close()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app
