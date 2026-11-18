"""Kalender-Abo (iCal, RFC 5545) für Therapeut/innen: eigene Termine −30 bis +120 Tage, im Telefon-/Outlook-Kalender abonnierbar.

Datenschutz: Standard nur Initialen der Patient/innen («Termin L. M.»), wahlweise gar keine Namen («Termin»). Keine Diagnosen,
keine Notizen. Der Link ist geheim (32 Zufallsbytes) und jederzeit widerrufbar; gespeichert wird nur sein Hash.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import from_iso, to_utc_iso

PAST_DAYS = 30
FUTURE_DAYS = 120


def token_hash(token: str) -> str:
    return hashlib.sha256(f"ical:{token}".encode()).hexdigest()


def create(db: Database, label: str | None, detail: str) -> str:
    token = secrets.token_urlsafe(32)
    tid = db.execute("INSERT INTO ical_tokens (practice_id, user_id, token_hash, label, detail, created_at) VALUES (?,?,?,?,?,?)",
                     (current_practice_id(), current_user_id(), token_hash(token), (label or "")[:60] or None,
                      detail if detail in ("initials", "none") else "initials", utcnow()))
    audit(db, "create", "ical_token", tid)
    return token


def mine(db: Database):
    return db.query("SELECT id, label, detail, created_at, last_used_at FROM ical_tokens WHERE practice_id = ? AND user_id = ?"
                    " AND revoked_at IS NULL ORDER BY id DESC", (current_practice_id(), current_user_id()))


def revoke(db: Database, tid: int, *, any_user: bool = False) -> bool:
    sql = "UPDATE ical_tokens SET revoked_at = ? WHERE id = ? AND practice_id = ? AND revoked_at IS NULL"
    params: list = [utcnow(), tid, current_practice_id()]
    if not any_user:
        sql += " AND user_id = ?"
        params.append(current_user_id())
    db.execute(sql, params)
    audit(db, "revoke", "ical_token", tid)
    return True


def lookup(db: Database, token: str):
    """Ohne Praxis-Kontext: Link → (practice_id, user_id, detail) oder None. Inaktive Konten liefern nichts."""
    if not token or len(token) > 100:
        return None
    return db.one("SELECT t.id, t.practice_id, t.user_id, t.detail, t.last_used_at FROM ical_tokens t JOIN users u ON u.id = t.user_id"
                  " WHERE t.token_hash = ? AND t.revoked_at IS NULL AND u.active = 1", (token_hash(token),))


def touch(db: Database, row) -> None:
    last = from_iso(row["last_used_at"]) if row["last_used_at"] else None
    if last is None or datetime.now(timezone.utc) - last > timedelta(hours=1):
        db.execute("UPDATE ical_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"]))


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Zeilen > 75 Bytes falten (RFC 5545, 3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(iso: str) -> str:
    return from_iso(iso).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build(db: Database, *, practice_id: int, user_id: int, detail: str, calendar_name: str, lang: str = "de") -> str:
    now = datetime.now(timezone.utc)
    lo, hi = to_utc_iso(now - timedelta(days=PAST_DAYS)), to_utc_iso(now + timedelta(days=FUTURE_DAYS))
    rows = db.query(
        "SELECT a.id, a.starts_at, a.ends_at, a.status, a.title, a.updated_at, p.first_name, p.last_name, l.name AS location_name,"
        " r.name AS room_name, tt.name_de, tt.name_fr FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id"
        " LEFT JOIN locations l ON l.id = a.location_id LEFT JOIN rooms r ON r.id = a.room_id"
        " LEFT JOIN treatment_types tt ON tt.id = a.treatment_type_id WHERE a.practice_id = ? AND a.therapist_id = ?"
        " AND a.starts_at >= ? AND a.starts_at < ? AND a.status IN ('booked', 'arrived', 'done', 'blocked') ORDER BY a.starts_at",
        (practice_id, user_id, lo, hi))
    word = "Rendez-vous" if lang == "fr" else "Termin"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NSystem GmbH//N-Fizyo//DE", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(calendar_name)}", "X-PUBLISHED-TTL:PT15M", "REFRESH-INTERVAL;VALUE=DURATION:PT15M"]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for a in rows:
        if a["status"] == "blocked":
            summary = a["title"] or ("Bloqué" if lang == "fr" else "Blockiert")
        elif detail == "initials" and a["first_name"]:
            summary = f"{word} {a['first_name'][:1]}. {(a['last_name'] or '')[:1]}."
        else:
            summary = word
        typ = (a["name_fr"] if lang == "fr" else a["name_de"]) if a["name_de"] else None
        if typ and a["status"] != "blocked":
            summary += f" · {typ}"
        where = ", ".join(x for x in (a["location_name"], a["room_name"]) if x)
        lines += ["BEGIN:VEVENT", f"UID:nfizyo-{practice_id}-{a['id']}@n-fizyo", f"DTSTAMP:{stamp}", f"DTSTART:{_utc(a['starts_at'])}",
                  f"DTEND:{_utc(a['ends_at'])}", f"SUMMARY:{_esc(summary)}", f"LAST-MODIFIED:{_utc(a['updated_at'])}",
                  "TRANSP:OPAQUE", "CLASS:PRIVATE"]
        if where:
            lines.append(f"LOCATION:{_esc(where)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"
