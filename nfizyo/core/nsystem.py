"""Kern: NSystem-Betrieb auf dem Praxis-Server (0.9.0).

- Supportzugang: nur die Leitung öffnet ihn (1–8 h, Standard 4 h), sieht ihn mit Restzeit und kann ihn früher schliessen.
  Die App legt dafür einen Auftrag für den Runner (root) ab: runner/local/<id>.json, mit HMAC aus NFIZYO_SECRET_KEY signiert.
  Der Runner prüft den Auftrag, baut den WireGuard-Tunnel zum NSystem-Hub auf (nur dann ist SSH über den Tunnel möglich),
  schliesst ihn bei Ablauf selbst – auch wenn die App nicht läuft – und schreibt jeden Schritt als Ereignis (runner/events/).
  NSystem kann den Zugang nicht öffnen (kein Befehl dafür), nur früher schliessen.
- Protokoll box_events: jeder NSystem-Vorgang (Supportzugang, Verbindungen, Befehle über SSH, Anfragen, Antworten, Hinweise).
  Die Leitung sieht es unter Einstellungen › NSystem; an NSystem gehen nur Art und Zeit (ohne Namen).
- Support-Anfragen (Hilfe › Support anfragen): vorher auf Patientendaten geprüft (core/patientdata.py); blockierter Text wird
  nie gespeichert. Die Antwort von NSystem kommt mit der nächsten Meldung (signiert) zurück.
- Wartungshinweise von NSystem: Banner für das Personal im angegebenen Zeitfenster, pro Person ausblendbar.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import crypto
from .db import Database, utcnow

AREAS = ("agenda", "documentation", "billing", "patientapp", "technical", "other")
URGENCIES = ("low", "normal", "urgent")
SUPPORT_HOURS = tuple(range(1, 9))
SUPPORT_DEFAULT_HOURS = 4
LOCAL_ACTIONS = ("support_open", "support_close")
LOCAL_FIELDS = ("id", "action", "hours", "at")
LOCAL_MAX_AGE = 600          # Sekunden: so lange gilt ein Auftrag der App an den Runner
PURPOSE_LOCAL = "runner-local"
MAX_BODY = 2000
# Ereignisse, deren Detail (ohne Namen) an NSystem gemeldet wird – alles andere nur Art und Zeit
REPORT_DETAIL = {"support_opened": ("hours", "until"), "support_closed": ("reason",), "support_command": ("cmd",),
                 "support_ssh_login": (), "support_ssh_logout": (), "support_tunnel_up": (), "support_open_failed": ("error",),
                 "service_action": ("action", "result", "version"), "service_denied": ("why", "action")}


# --------------------------------------------------------------------------- Ordner des Runners
def runner_paths(settings) -> dict[str, Path]:
    base = settings.data_dir / "runner"
    return {"base": base, "spool": base / "spool", "results": base / "results", "local": base / "local",
            "events": base / "events", "status": base / "status.json"}


def _utc(value: str) -> str:
    """ISO-Zeit → UTC im Format von utcnow() (damit Vergleiche als Text stimmen)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- Aufträge App → Runner (signiert)
def local_mac(secret_key: bytes, req: dict) -> str:
    body = json.dumps({k: req.get(k) for k in LOCAL_FIELDS}, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(crypto.derive(secret_key, PURPOSE_LOCAL), body, hashlib.sha256).hexdigest()


def check_local_request(secret_key: bytes, req: dict, now: float | None = None) -> str | None:
    """None = gültig, sonst Grund. Prüft Signatur, Alter, Aktion und Dauer (1–8 h)."""
    if not isinstance(req, dict) or not secret_key:
        return "invalid"
    if not hmac.compare_digest(local_mac(secret_key, req), str(req.get("mac") or "")):
        return "bad_signature"
    try:
        at = float(req.get("at") or 0)
    except (TypeError, ValueError):
        return "invalid"
    if abs((now or time.time()) - at) > LOCAL_MAX_AGE:
        return "expired"
    action = req.get("action")
    if action not in LOCAL_ACTIONS:
        return "not_allowed"
    hours = req.get("hours")
    if action == "support_open" and (not isinstance(hours, int) or isinstance(hours, bool) or hours not in SUPPORT_HOURS):
        return "bad_hours"
    if action == "support_close" and hours not in (None, 0):
        return "bad_hours"
    return None


def _write_local(settings, action: str, hours: int | None) -> str:
    paths = runner_paths(settings)
    paths["local"].mkdir(parents=True, exist_ok=True)
    req = {"id": f"sup-{uuid.uuid4()}", "action": action, "hours": hours, "at": int(time.time())}
    req["mac"] = local_mac(settings.secret_key, req)
    tmp = paths["local"] / f".{req['id']}.tmp"
    tmp.write_text(json.dumps(req))
    tmp.rename(paths["local"] / f"{req['id']}.json")
    return req["id"]


# --------------------------------------------------------------------------- Protokoll
def log_event(db: Database, kind: str, *, detail: dict | None = None, practice_id: int | None = None, user_id: int | None = None,
              uid: str | None = None, at: str | None = None) -> bool:
    """Ereignis anhängen (doppelte uid wird ignoriert). True = neu."""
    uid = uid or f"app-{uuid.uuid4()}"
    if db.value("SELECT 1 FROM box_events WHERE uid = ?", (uid,)):
        return False
    db.execute("INSERT INTO box_events (uid, at, kind, practice_id, user_id, detail) VALUES (?,?,?,?,?,?)",
               (uid[:80], at or utcnow(), kind[:40], practice_id, user_id,
                json.dumps(detail, ensure_ascii=False, sort_keys=True)[:1000] if detail else None))
    return True


def events(db: Database, practice_id: int, limit: int = 80) -> list[dict]:
    """Protokoll für die Leitung: Vorgänge der ganzen Box, der eigenen Praxis und jeder Supportzugang (gilt für den ganzen
    Server, auch wenn eine andere Praxis ihn geöffnet hat). Namen nur aus der eigenen Praxis."""
    rows = db.query("SELECT e.*, u.name AS user_name FROM box_events e LEFT JOIN users u ON u.id = e.user_id AND u.practice_id = ?"
                    " WHERE e.practice_id IS NULL OR e.practice_id = ? OR e.kind LIKE 'support^_%' ESCAPE '^'"
                    " ORDER BY e.at DESC, e.id DESC LIMIT ?", (practice_id, practice_id, limit))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(r["detail"]) if r["detail"] else {}
        except json.JSONDecodeError:
            d["data"] = {}
        out.append(d)
    return out


def events_for_report(db: Database, limit: int = 100) -> list[dict]:
    """Noch nicht gemeldete Ereignisse für die NSystem-Übersicht: Art, Zeit und feste Felder – keine Namen, keine Praxis-IDs."""
    out = []
    for r in db.query("SELECT uid, at, kind, detail FROM box_events WHERE reported = 0 ORDER BY id LIMIT ?", (limit,)):
        item = {"uid": r["uid"], "at": r["at"], "kind": r["kind"]}
        allowed = REPORT_DETAIL.get(r["kind"])
        if allowed and r["detail"]:
            try:
                raw = json.loads(r["detail"])
            except json.JSONDecodeError:
                raw = {}
            item["detail"] = {k: raw.get(k) for k in allowed if isinstance(raw.get(k), (str, int, float)) and not isinstance(raw.get(k), bool)}
        out.append(item)
    return out


def mark_events_reported(db: Database, uids: list[str]) -> None:
    for i in range(0, len(uids), 200):
        part = uids[i:i + 200]
        db.execute(f"UPDATE box_events SET reported = 1 WHERE uid IN ({','.join('?' for _ in part)})", part)


# --------------------------------------------------------------------------- Supportzugang
def support_configured(settings) -> bool:
    return bool(settings.support_hub and settings.support_hub_pubkey and settings.support_address and settings.support_ssh_keys)


def support_status(settings) -> dict:
    """Zustand laut Runner (runner/status.json › support): open, until (Unix-Zeit), opened_at, configured, ready."""
    try:
        raw = json.loads(runner_paths(settings)["status"].read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    sup = raw.get("support") if isinstance(raw.get("support"), dict) else {}
    runner_fresh = bool(raw.get("updated_at")) and time.time() - float(raw.get("updated_at") or 0) < 600
    until = sup.get("until") if isinstance(sup.get("until"), (int, float)) else None
    is_open = bool(sup.get("open")) and until is not None and until > time.time()
    return {"open": is_open, "until": until, "until_iso": _iso(until) if is_open else None,
            "remaining": max(0, int(until - time.time())) if is_open else 0,
            "opened_at": sup.get("opened_at") if isinstance(sup.get("opened_at"), (int, float)) else None,
            "runner": runner_fresh, "ready": bool(sup.get("ready")), "configured": support_configured(settings)}


def support_allowed(db: Database, settings) -> bool:
    """Supportzugang gilt für den ganzen Server. Mit mehreren (echten) Praxen auf einem Server nicht über die App."""
    return settings.demo or db.value("SELECT COUNT(*) FROM practices", (), 0) <= 1


def pending_session(db: Database):
    return db.one("SELECT * FROM box_support_sessions WHERE status = 'requested' ORDER BY id DESC LIMIT 1")


def open_support(db: Database, settings, *, practice_id: int, user_id: int, hours: int) -> str:
    """Leitung öffnet den Supportzugang (Einverständnis). Gibt die Auftrags-ID zurück."""
    if hours not in SUPPORT_HOURS:
        raise ValueError("hours")
    rid = _write_local(settings, "support_open", hours)
    db.execute("INSERT INTO box_support_sessions (request_id, practice_id, opened_by, hours, requested_at) VALUES (?,?,?,?,?)",
               (rid, practice_id, user_id, hours, utcnow()))
    log_event(db, "support_requested", detail={"hours": hours, "request_id": rid}, practice_id=practice_id, user_id=user_id)
    return rid


def close_support(db: Database, settings, *, practice_id: int, user_id: int) -> str:
    rid = _write_local(settings, "support_close", None)
    db.execute("UPDATE box_support_sessions SET closed_by = ? WHERE status IN ('requested', 'open') AND closed_by IS NULL", (user_id,))
    log_event(db, "support_close_requested", detail={"request_id": rid}, practice_id=practice_id, user_id=user_id)
    return rid


def ingest_runner_events(db: Database, settings) -> int:
    """Ereignisse des Runners (runner/events/*.json) ins Protokoll übernehmen und den Supportzugang nachführen."""
    folder = runner_paths(settings)["events"]
    if not folder.exists():
        return 0
    n = 0
    for f in sorted(folder.glob("*.json")):
        try:
            ev = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            f.unlink(missing_ok=True)
            continue
        kind = str(ev.get("kind") or "")[:40]
        detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
        at = _iso(ev.get("at")) if isinstance(ev.get("at"), (int, float)) else utcnow()
        if kind and log_event(db, kind, detail=detail, uid=str(ev.get("uid") or f.stem), at=at):
            n += 1
            _apply_support_event(db, kind, detail, at)
        f.unlink(missing_ok=True)
    return n


def _apply_support_event(db: Database, kind: str, detail: dict, at: str) -> None:
    rid = str(detail.get("request_id") or "")
    if kind == "support_opened" and rid:
        db.execute("UPDATE box_support_sessions SET status = 'open', opened_at = ?, until = ? WHERE request_id = ?",
                   (at, _iso(detail.get("until")) if isinstance(detail.get("until"), (int, float)) else None, rid))
    elif kind in ("support_open_failed", "support_request_rejected") and rid:
        db.execute("UPDATE box_support_sessions SET status = 'failed', closed_at = ?, close_reason = 'failed', error = ?"
                   " WHERE request_id = ? AND status = 'requested'", (at, str(detail.get("error") or detail.get("reason") or "")[:200], rid))
    elif kind == "support_closed":
        db.execute("UPDATE box_support_sessions SET status = 'closed', closed_at = ?, close_reason = ? WHERE status IN ('requested', 'open')",
                   (at, str(detail.get("reason") or "")[:40]))


def sessions(db: Database, limit: int = 10):
    return db.query("SELECT * FROM box_support_sessions ORDER BY id DESC LIMIT ?", (limit,))


# --------------------------------------------------------------------------- Support-Anfragen
def create_request(db: Database, *, practice_id: int, user_id: int, area: str, urgency: str, body: str, with_access: bool) -> str:
    """Nur aufrufen, wenn core.patientdata.scan(body, …) nichts gefunden hat (die Seite prüft das vorher)."""
    if area not in AREAS or urgency not in URGENCIES or not body.strip():
        raise ValueError("invalid")
    uid = f"req-{uuid.uuid4()}"
    db.execute("INSERT INTO box_support_requests (uid, practice_id, user_id, area, urgency, body, with_access, created_at)"
               " VALUES (?,?,?,?,?,?,?,?)", (uid, practice_id, user_id, area, urgency, body.strip()[:MAX_BODY], 1 if with_access else 0, utcnow()))
    log_event(db, "request_created", detail={"area": area, "urgency": urgency}, practice_id=practice_id, user_id=user_id)
    return uid


def requests_for(db: Database, practice_id: int, limit: int = 50):
    return db.query("SELECT r.*, u.name AS user_name FROM box_support_requests r LEFT JOIN users u ON u.id = r.user_id"
                    " WHERE r.practice_id = ? ORDER BY r.created_at DESC LIMIT ?", (practice_id, limit))


def unread_replies(db: Database, practice_id: int, user_id: int | None = None) -> int:
    """Neue Antworten von NSystem: für die Leitung alle der Praxis (user_id=None), sonst die eigenen."""
    return db.value("SELECT COUNT(*) FROM box_support_requests WHERE practice_id = ? AND (? IS NULL OR user_id = ?)"
                    " AND reply_at IS NOT NULL AND reply_seen_at IS NULL", (practice_id, user_id, user_id), 0)


def mark_replies_seen(db: Database, practice_id: int, user_id: int | None = None) -> None:
    db.execute("UPDATE box_support_requests SET reply_seen_at = ? WHERE practice_id = ? AND (? IS NULL OR user_id = ?)"
               " AND reply_at IS NOT NULL AND reply_seen_at IS NULL", (utcnow(), practice_id, user_id, user_id))


def outgoing_requests(db: Database, limit: int = 20) -> list[dict]:
    """Noch nicht übermittelte Anfragen für die nächste Meldung (Text ist geprüft; Absender = Name und Rolle des Personals)."""
    rows = db.query("SELECT r.*, u.name AS user_name, u.roles AS user_roles, p.slug AS practice_slug FROM box_support_requests r"
                    " JOIN users u ON u.id = r.user_id JOIN practices p ON p.id = r.practice_id"
                    " WHERE r.sent_at IS NULL ORDER BY r.id LIMIT ?", (limit,))
    return [{"uid": r["uid"], "practice": r["practice_slug"], "from_name": r["user_name"], "from_role": r["user_roles"],
             "area": r["area"], "urgency": r["urgency"], "body": r["body"], "with_access": bool(r["with_access"]),
             "created_at": r["created_at"]} for r in rows]


def mark_requests_sent(db: Database, uids: list[str]) -> None:
    for uid in uids:
        row = db.one("SELECT practice_id, area FROM box_support_requests WHERE uid = ? AND sent_at IS NULL", (uid,))
        if row is None:
            continue
        db.execute("UPDATE box_support_requests SET sent_at = ? WHERE uid = ?", (utcnow(), uid))
        log_event(db, "request_sent", detail={"area": row["area"]}, practice_id=row["practice_id"])


def known_revs(db: Database, days: int = 30) -> dict[str, int]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    return {r["uid"]: r["rev"] for r in db.query("SELECT uid, rev FROM box_support_requests WHERE sent_at IS NOT NULL"
                                                  " AND (status != 'done' OR created_at >= ?)", (since,))}


def apply_reply(db: Database, item: dict) -> bool:
    """Geprüfte (signierte) Antwort von NSystem übernehmen. True = geändert."""
    uid, rev = str(item.get("uid") or ""), item.get("rev")
    row = db.one("SELECT * FROM box_support_requests WHERE uid = ?", (uid,))
    if row is None or not isinstance(rev, int) or rev <= row["rev"] or item.get("status") not in ("new", "in_progress", "done"):
        return False
    reply = str(item.get("reply") or "").strip()[:4000] or None
    new_reply = bool(reply) and reply != row["reply"]
    db.execute("UPDATE box_support_requests SET status = ?, reply = ?, reply_by = ?, reply_at = ?, rev = ?,"
               " reply_seen_at = CASE WHEN ? THEN NULL ELSE reply_seen_at END WHERE uid = ?",
               (item["status"], reply, str(item.get("reply_by") or "")[:120] or None,
                utcnow() if new_reply else row["reply_at"], rev, 1 if new_reply else 0, uid))
    log_event(db, "reply_received" if new_reply else "request_status", detail={"status": item["status"]}, practice_id=row["practice_id"])
    return True


# --------------------------------------------------------------------------- Wartungshinweise
def sync_notices(db: Database, notices: list[dict]) -> int:
    """Geprüfte (signierte) Hinweise der Übersicht übernehmen. Was nicht mehr in der Liste steht, ist zurückgezogen/abgelaufen."""
    seen = set()
    n = 0
    for item in notices:
        nid = str(item.get("id") or "")[:64]
        de, fr = str(item.get("text_de") or "").strip()[:600], str(item.get("text_fr") or "").strip()[:600]
        try:
            start, end = _utc(str(item.get("starts_at") or "")), _utc(str(item.get("ends_at") or ""))
        except ValueError:
            continue
        if not nid or not de or not fr:
            continue
        seen.add(nid)
        row = db.one("SELECT * FROM box_notices WHERE id = ?", (nid,))
        if row is None:
            db.execute("INSERT INTO box_notices (id, text_de, text_fr, starts_at, ends_at, received_at) VALUES (?,?,?,?,?,?)",
                       (nid, de, fr, start, end, utcnow()))
            log_event(db, "notice_received", detail={"id": nid, "starts_at": start, "ends_at": end})
            n += 1
        elif (row["text_de"], row["text_fr"], row["starts_at"], row["ends_at"]) != (de, fr, start, end) or row["withdrawn_at"]:
            db.execute("UPDATE box_notices SET text_de = ?, text_fr = ?, starts_at = ?, ends_at = ?, withdrawn_at = NULL WHERE id = ?",
                       (de, fr, start, end, nid))
            n += 1
    for row in db.query("SELECT id FROM box_notices WHERE withdrawn_at IS NULL"):
        if row["id"] not in seen:
            db.execute("UPDATE box_notices SET withdrawn_at = ? WHERE id = ?", (utcnow(), row["id"]))
    return n


def active_notices(db: Database, user, lang: str, now: str | None = None) -> list[dict]:
    """Hinweise im Zeitfenster, nicht zurückgezogen und von dieser Person nicht ausgeblendet (nur Personal)."""
    if user is None:
        return []
    now = now or utcnow()
    rows = db.query("SELECT n.* FROM box_notices n WHERE n.withdrawn_at IS NULL AND n.starts_at <= ? AND n.ends_at > ?"
                    " AND NOT EXISTS (SELECT 1 FROM box_notice_dismissals d WHERE d.notice_id = n.id AND d.user_id = ?)"
                    " ORDER BY n.starts_at", (now, now, user["id"]))
    return [{"id": r["id"], "text": r["text_fr"] if lang == "fr" else r["text_de"], "ends_at": r["ends_at"]} for r in rows]


def dismiss_notice(db: Database, notice_id: str, user) -> bool:
    if not db.value("SELECT 1 FROM box_notices WHERE id = ?", (notice_id,)):
        return False
    db.execute("INSERT OR IGNORE INTO box_notice_dismissals (notice_id, user_id, practice_id, dismissed_at) VALUES (?,?,?,?)",
               (notice_id, user["id"], user["practice_id"], utcnow()))
    return True
