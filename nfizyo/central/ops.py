"""Betrieb über die NSystem-Übersicht (0.9.0): Protokoll der Boxen, Support-Anfragen, Wartungshinweise, Supportzugang,
eigene Sicherung. Alles ohne Patientendaten – Anfragen werden hier ein zweites Mal geprüft (core/patientdata.py, nur Muster);
blockierter Text wird nie gespeichert.
"""
from __future__ import annotations

import ipaddress
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from ..agent import signing
from ..core import patientdata
from ..core.db import Database, utcnow

AREAS = {"agenda": "Agenda", "documentation": "Dokumentation", "billing": "Abrechnung", "patientapp": "Patienten-App",
         "technical": "Technik", "other": "Anderes"}
URGENCIES = {"low": "kann warten", "normal": "normal", "urgent": "dringend"}
STATUSES = {"new": "neu", "in_progress": "in Bearbeitung", "done": "erledigt"}
EVENT_LABELS = {
    "support_requested": "Leitung fordert Supportzugang an", "support_opened": "Supportzugang offen", "support_closed": "Supportzugang geschlossen",
    "support_open_failed": "Supportzugang: Öffnen fehlgeschlagen", "support_request_rejected": "Auftrag an Runner abgelehnt",
    "support_tunnel_up": "Tunnel verbunden", "support_ssh_login": "SSH-Anmeldung (Support)", "support_ssh_logout": "SSH-Abmeldung",
    "support_ssh_denied": "SSH abgelehnt", "support_command": "Befehl über SSH", "support_close_requested": "Leitung schliesst Supportzugang",
    "request_created": "Support-Anfrage erstellt", "request_sent": "Support-Anfrage übermittelt", "reply_received": "Antwort erhalten",
    "request_status": "Status der Anfrage", "notice_received": "Wartungshinweis erhalten",
    "service_action": "Servicekabel vor Ort", "service_denied": "Servicekabel: Anfrage abgewiesen",
}
CLOSE_REASONS = {"early": "von der Leitung", "expired": "abgelaufen", "nsystem": "von NSystem", "failed": "Fehler", "stale": "aufgeräumt"}
UPDATE_NOTICE_DE = ("Wartung: Am {date} um {time} wird N-Fizyo auf Version {version} aktualisiert. N-Fizyo ist dann etwa "
                    "15 Minuten nicht erreichbar. Bitte speichern Sie Ihre Arbeit vorher.")
UPDATE_NOTICE_FR = ("Maintenance : le {date} à {time}, N-Fizyo sera mis à jour (version {version}). N-Fizyo sera indisponible "
                    "pendant environ 15 minutes. Veuillez enregistrer votre travail avant.")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def get_state(db: Database, key: str) -> str | None:
    return db.value("SELECT value FROM central_state WHERE key = ?", (key,))


def set_state(db: Database, key: str, value: str | None) -> None:
    db.execute("INSERT INTO central_state (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET"
               " value = excluded.value, updated_at = excluded.updated_at", (key, value, utcnow()))


# --------------------------------------------------------------------------- Meldung einer Box verarbeiten
def ingest_events(db: Database, box_id: str, events) -> list[dict]:
    """Protokoll der Box (nur Art, Zeit, feste Felder). Gibt die neuen Ereignisse zurück."""
    new = []
    for e in events[:200] if isinstance(events, list) else []:
        if not isinstance(e, dict):
            continue
        uid, kind = str(e.get("uid") or "")[:80], str(e.get("kind") or "")[:40]
        if not uid or not kind or db.value("SELECT 1 FROM box_events WHERE box_id = ? AND uid = ?", (box_id, uid)):
            continue
        detail = e.get("detail") if isinstance(e.get("detail"), dict) else None
        clean = {k: v for k, v in (detail or {}).items() if k in ("hours", "until", "reason", "cmd", "error")
                 and isinstance(v, (str, int, float)) and not isinstance(v, bool) and len(str(v)) <= 80}
        db.execute("INSERT INTO box_events (box_id, uid, at, kind, detail, received_at) VALUES (?,?,?,?,?,?)",
                   (box_id, uid, str(e.get("at") or utcnow())[:40], kind, json.dumps(clean) if clean else None, utcnow()))
        new.append({"kind": kind, "detail": clean})
    return new


def ingest_requests(db: Database, box_id: str, requests) -> list[dict]:
    """Neue Support-Anfragen. Zweite Prüfung auf Patientendaten (Muster): dann wird der Text NICHT gespeichert."""
    new = []
    for r in requests[:20] if isinstance(requests, list) else []:
        if not isinstance(r, dict):
            continue
        uid = str(r.get("uid") or "")[:80]
        if not uid or db.value("SELECT 1 FROM support_requests WHERE box_id = ? AND uid = ?", (box_id, uid)):
            continue
        area = r.get("area") if r.get("area") in AREAS else "other"
        urgency = r.get("urgency") if r.get("urgency") in URGENCIES else "normal"
        body = str(r.get("body") or "")[:2000]
        found = patientdata.scan(body)
        db.execute("INSERT INTO support_requests (box_id, uid, practice, from_name, from_role, area, urgency, body, blocked, with_access,"
                   " created_at, received_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (box_id, uid, str(r.get("practice") or "")[:60], str(r.get("from_name") or "")[:120], str(r.get("from_role") or "")[:60],
                    area, urgency, None if found else body, ",".join(found) or None, 1 if r.get("with_access") else 0,
                    str(r.get("created_at") or "")[:40], utcnow(), utcnow()))
        new.append({"area": area, "urgency": urgency, "blocked": bool(found), "with_access": bool(r.get("with_access"))})
    return new


def replies_for(db: Database, box_id: str, known, private_pem: bytes) -> list[dict]:
    """Antworten/Status, die die Box noch nicht hat (Stand rev), signiert für genau diese Box."""
    known = known if isinstance(known, dict) else {}
    out = []
    for r in db.query("SELECT * FROM support_requests WHERE box_id = ? AND rev > 0", (box_id,)):
        have = known.get(r["uid"])
        if isinstance(have, int) and have >= r["rev"]:
            continue
        if r["uid"] not in known:
            continue                       # Box kennt die Anfrage nicht (mehr)
        data = {"uid": r["uid"], "rev": r["rev"], "status": r["status"], "reply": r["reply"], "reply_by": r["reply_by"]}
        out.append(dict(data, signature=signing.sign_payload(private_pem, "support_reply", dict(data, box_id=box_id))))
    return out


def answer(db: Database, req_id: int, *, status: str, reply: str | None, staff_name: str) -> bool:
    row = db.one("SELECT * FROM support_requests WHERE id = ?", (req_id,))
    if row is None or status not in STATUSES:
        return False
    reply = (reply or "").strip()[:4000] or row["reply"]
    changed_reply = reply != row["reply"]
    db.execute("UPDATE support_requests SET status = ?, reply = ?, reply_by = ?, reply_at = ?, rev = rev + 1, updated_at = ? WHERE id = ?",
               (status, reply, staff_name if changed_reply else row["reply_by"], utcnow() if changed_reply else row["reply_at"],
                utcnow(), req_id))
    return True


# --------------------------------------------------------------------------- Wartungshinweise
def create_notice(db: Database, *, text_de: str, text_fr: str, starts_at: datetime, ends_at: datetime, boxes: list[str] | None,
                  staff_id: int | None) -> str:
    if not text_de.strip() or not text_fr.strip() or ends_at <= starts_at:
        raise ValueError("Hinweis unvollständig")
    nid = str(uuid.uuid4())
    db.execute("INSERT INTO notices (id, text_de, text_fr, starts_at, ends_at, all_boxes, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (nid, text_de.strip()[:600], text_fr.strip()[:600], _iso(starts_at), _iso(ends_at), 0 if boxes else 1, staff_id, utcnow()))
    for b in boxes or []:
        db.execute("INSERT OR IGNORE INTO notice_boxes (notice_id, box_id) VALUES (?,?)", (nid, b))
    return nid


def update_notice_texts(version: str, at_local: datetime) -> tuple[str, str]:
    d, t = at_local.strftime("%d.%m.%Y"), at_local.strftime("%H:%M")
    return UPDATE_NOTICE_DE.format(date=d, time=t, version=version), UPDATE_NOTICE_FR.format(date=d, time=t, version=version)


def notices_for(db: Database, box_id: str, private_pem: bytes) -> list[dict]:
    """Alle laufenden und kommenden Hinweise für diese Box, einzeln signiert (Box-ID in der Signatur)."""
    now = utcnow()
    rows = db.query("SELECT * FROM notices n WHERE n.withdrawn_at IS NULL AND n.ends_at > ? AND (n.all_boxes = 1 OR EXISTS"
                    " (SELECT 1 FROM notice_boxes nb WHERE nb.notice_id = n.id AND nb.box_id = ?)) ORDER BY n.starts_at", (now, box_id))
    out = []
    for n in rows:
        data = {"id": n["id"], "text_de": n["text_de"], "text_fr": n["text_fr"], "starts_at": n["starts_at"], "ends_at": n["ends_at"]}
        out.append(dict(data, signature=signing.sign_payload(private_pem, "notice", dict(data, box_id=box_id))))
    return out


# --------------------------------------------------------------------------- Supportzugang: Tunnel-Adressen
def support_network(settings) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(settings.support_net, strict=False)


def hub_ip(settings) -> str:
    return str(next(support_network(settings).hosts()))


def assign_peer(db: Database, settings, box_id: str, pubkey: str) -> str:
    """Box als Peer am Hub: Adresse vergeben (ab .10), öffentlichen Schlüssel merken. Gibt die Tunnel-Adresse zurück."""
    from ..agent.support import WGKEY_RX

    if not WGKEY_RX.fullmatch(pubkey):
        raise ValueError("WireGuard-Schlüssel ungültig (44 Zeichen Base64)")
    box = db.one("SELECT * FROM boxes WHERE id = ?", (box_id,))
    if box is None:
        raise ValueError("Box unbekannt")
    if db.value("SELECT 1 FROM boxes WHERE wg_pubkey = ? AND id != ?", (pubkey, box_id)):
        raise ValueError("Schlüssel gehört schon einer anderen Box")
    ip = box["wg_ip"]
    if not ip:
        used = {r["wg_ip"] for r in db.query("SELECT wg_ip FROM boxes WHERE wg_ip IS NOT NULL")}
        net = support_network(settings)
        ip = next((str(h) for i, h in enumerate(net.hosts()) if i >= 9 and str(h) not in used), None)
        if ip is None:
            raise ValueError("Tunnel-Netz voll")
    db.execute("UPDATE boxes SET wg_pubkey = ?, wg_ip = ? WHERE id = ?", (pubkey, ip, box_id))
    return ip


def peers_conf(db: Database) -> str:
    """[Peer]-Abschnitte für den Hub – nur öffentliche Schlüssel und Adressen (keine Geheimnisse)."""
    lines = ["# N-Fizyo Support-Hub: Peers (erzeugt von «python -m nfizyo.central support-peer», nicht von Hand ändern)"]
    for b in db.query("SELECT id, name, wg_pubkey, wg_ip FROM boxes WHERE wg_pubkey IS NOT NULL AND wg_ip IS NOT NULL AND archived = 0"
                      " ORDER BY wg_ip"):
        lines += ["", f"# {b['id']}", "[Peer]", f"PublicKey = {b['wg_pubkey']}", f"AllowedIPs = {b['wg_ip']}/32"]
    return "\n".join(lines) + "\n"


def support_state(report: dict | None) -> dict:
    sup = ((report or {}).get("runner") or {}).get("support") or {}
    until = sup.get("until") if isinstance(sup.get("until"), (int, float)) else None
    is_open = bool(sup.get("open")) and until is not None and until > time.time()
    return {"open": is_open, "until": until, "configured": bool(sup.get("configured")), "ready": bool(sup.get("ready"))}


# --------------------------------------------------------------------------- Sicherung des Panels
def backup_state(settings) -> dict:
    """Ergebnis der letzten Sicherung des Panels (deploy/n-fizyo-backup schreibt backup-status.json)."""
    try:
        raw = json.loads(settings.backup_status_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"known": False}
    local = raw.get("local") if isinstance(raw.get("local"), dict) else {}
    off = raw.get("offsite") if isinstance(raw.get("offsite"), dict) else {}
    at = local.get("at") if isinstance(local.get("at"), (int, float)) else None
    age_h = (time.time() - at) / 3600 if at else None
    problem = None
    if local.get("state") == "failed":
        problem = "Sicherung des Panels fehlgeschlagen"
    elif age_h is None or age_h > 30:
        problem = "Sicherung des Panels zu alt"
    elif off.get("state") == "failed":
        problem = f"Externe Sicherung des Panels fehlgeschlagen ({off.get('error') or 'unbekannt'})"
    elif off.get("state") != "ok":
        problem = "Sicherung des Panels nur lokal (keine externe Kopie)"
    return {"known": True, "age_h": age_h, "local": local.get("state"), "offsite": off.get("state"), "problem": problem,
            "size_mb": local.get("size_mb")}


def schedule_time(text: str, tz) -> datetime | None:
    """«2026-11-12T21:00» (Zürcher Zeit aus dem Formular) → datetime mit Zeitzone. Leer = sofort."""
    text = (text or "").strip()
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def in_minutes(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)
