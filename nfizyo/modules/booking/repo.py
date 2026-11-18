"""Online-Termine: freie Zeiten berechnen, Anfragen (mit SMS-Code) speichern, bestätigen oder ablehnen.

Freie Zeit = Arbeitszeit einer online buchbaren Person (Agenda) − Termine/Blocker − offene Anfragen − Vorlaufzeit
− Feiertage des Kantons, Schliessungen der Praxis und Abwesenheiten der Person (Ferien, Krankheit, Weiterbildung …).
Eine Anfrage hält ihre Zeit fest, solange sie offen ist (unbestätigt höchstens 15 Minuten). Erst der Empfang bestätigt:
dann entstehen Patient/in (falls neu) und Termin.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ...core import patients as core_patients
from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.sms import normalize_phone
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import TZ, from_iso, to_utc_iso
from ..agenda import api as agenda_api

CODE_MINUTES = 15
MAX_CODE_ATTEMPTS = 5
INSURANCE = ("KVG", "UVG", "IVG", "VVG", "privat")
DEFAULTS = {"enabled": 0, "days_ahead": 21, "lead_hours": 12, "slot_step": 15, "show_therapists": 0, "intro_de": None, "intro_fr": None}


# --------------------------------------------------------------------------- Einstellungen
def settings(db: Database) -> dict:
    row = db.one("SELECT * FROM booking_settings WHERE practice_id = ?", (current_practice_id(),))
    return {**DEFAULTS, **(dict(row) if row else {})}


def save_settings(db: Database, values: dict, type_ids: list[int], therapist_ids: list[int]) -> None:
    cur = settings(db)
    cur.update({k: values[k] for k in DEFAULTS if k in values})
    pid = current_practice_id()
    with db.tx():
        db.execute("INSERT INTO booking_settings (practice_id, enabled, days_ahead, lead_hours, slot_step, show_therapists, intro_de,"
                   " intro_fr, updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(practice_id) DO UPDATE SET enabled = excluded.enabled,"
                   " days_ahead = excluded.days_ahead, lead_hours = excluded.lead_hours, slot_step = excluded.slot_step,"
                   " show_therapists = excluded.show_therapists, intro_de = excluded.intro_de, intro_fr = excluded.intro_fr,"
                   " updated_at = excluded.updated_at",
                   (pid, cur["enabled"], cur["days_ahead"], cur["lead_hours"], cur["slot_step"], cur["show_therapists"], cur["intro_de"],
                    cur["intro_fr"], utcnow()))
        db.execute("DELETE FROM booking_types WHERE practice_id = ?", (pid,))
        valid_types = {t["id"] for t in agenda_api.lookup_tables(db)["types"]}
        for t in type_ids:
            if t in valid_types:
                db.execute("INSERT INTO booking_types (practice_id, treatment_type_id) VALUES (?,?)", (pid, t))
        db.execute("DELETE FROM booking_therapists WHERE practice_id = ?", (pid,))
        valid_users = {u["id"] for u in core_patients.therapists(db)}
        for u in therapist_ids:
            if u in valid_users:
                db.execute("INSERT INTO booking_therapists (practice_id, user_id) VALUES (?,?)", (pid, u))
        audit(db, "update", "booking_settings", pid, None, {"enabled": cur["enabled"], "types": len(type_ids), "therapists": len(therapist_ids)})


def type_ids(db: Database) -> set[int]:
    return {r["treatment_type_id"] for r in db.query("SELECT treatment_type_id FROM booking_types WHERE practice_id = ?", (current_practice_id(),))}


def therapist_ids(db: Database) -> set[int]:
    return {r["user_id"] for r in db.query("SELECT user_id FROM booking_therapists WHERE practice_id = ?", (current_practice_id(),))}


def bookable_types(db: Database) -> list:
    ids = type_ids(db)
    return [t for t in agenda_api.lookup_tables(db)["types"] if t["id"] in ids]


def bookable_therapists(db: Database) -> list:
    ids = therapist_ids(db)
    return [u for u in core_patients.therapists(db) if u["id"] in ids]


def locations(db: Database) -> list:
    return list(agenda_api.lookup_tables(db)["locations"])


# --------------------------------------------------------------------------- Freie Zeiten
@dataclass(frozen=True)
class Slot:
    start: datetime          # lokal (Europe/Zurich)
    therapist_id: int
    location_id: int | None


def _held(db: Database, therapist_id: int, start_utc: str, end_utc: str) -> list[tuple[str, str]]:
    """Zeiten, die offene Anfragen festhalten."""
    fresh = datetime.now(TZ) - timedelta(minutes=CODE_MINUTES)
    rows = db.query("SELECT starts_at, ends_at, status, created_at FROM booking_requests WHERE practice_id = ? AND therapist_id = ?"
                    " AND starts_at < ? AND ends_at > ? AND status IN ('unverified', 'requested')",
                    (current_practice_id(), therapist_id, end_utc, start_utc))
    return [(r["starts_at"], r["ends_at"]) for r in rows if r["status"] == "requested" or from_iso(r["created_at"]) >= fresh]


def free_slots(db: Database, type_row, *, location_id: int | None = None, start_day: date | None = None, days: int = 7,
               now: datetime | None = None) -> list[Slot]:
    st = settings(db)
    now = now or datetime.now(TZ)
    earliest = now + timedelta(hours=int(st["lead_hours"]))
    last_day = now.date() + timedelta(days=int(st["days_ahead"]))
    first = max(start_day or now.date(), now.date())
    minutes = int(type_row["minutes"] or 30)
    step = int(st["slot_step"])
    out: list[Slot] = []
    therapists = bookable_therapists(db)
    off = {t["id"]: agenda_api.off_times(db, t["id"], first, first + timedelta(days=days)) for t in therapists}
    for i in range(days):
        day = first + timedelta(days=i)
        if day > last_day:
            break
        day_start = datetime(day.year, day.month, day.day, tzinfo=TZ)
        lo, hi = to_utc_iso(day_start), to_utc_iso(day_start + timedelta(days=1))
        for t in therapists:
            hours = [h for h in agenda_api.working_hours(db, t["id"], day.weekday())
                     if not location_id or not h["location_id"] or h["location_id"] == location_id]
            if not hours:
                continue
            blocked = [(from_iso(a), from_iso(b)) for a, b in agenda_api.busy(db, t["id"], lo, hi) + _held(db, t["id"], lo, hi)]
            blocked += off[t["id"]]
            for h in hours:
                sh, sm = map(int, h["start_time"].split(":"))
                eh, em = map(int, h["end_time"].split(":"))
                cursor = day_start.replace(hour=sh, minute=sm)
                end_of_block = day_start.replace(hour=eh, minute=em)
                while cursor + timedelta(minutes=minutes) <= end_of_block:
                    slot_end = cursor + timedelta(minutes=minutes)
                    if cursor >= earliest and not any(b0 < slot_end and b1 > cursor for b0, b1 in blocked):
                        out.append(Slot(cursor, t["id"], h["location_id"] or location_id))
                    cursor += timedelta(minutes=step)
    return sorted(out, key=lambda s: (s.start, s.therapist_id))


def slot_still_free(db: Database, type_row, therapist_id: int, start: datetime, location_id: int | None,
                    exclude_request: int | None = None) -> bool:
    if therapist_id not in therapist_ids(db) or type_row["id"] not in type_ids(db):
        return False
    st = settings(db)
    if start < datetime.now(TZ) + timedelta(hours=int(st["lead_hours"])) or start.date() > datetime.now(TZ).date() + timedelta(days=int(st["days_ahead"])):
        return False
    hours = agenda_api.working_hours(db, therapist_id, start.weekday())
    end = start + timedelta(minutes=int(type_row["minutes"] or 30))
    s, e = start.strftime("%H:%M"), end.strftime("%H:%M")
    if not any(h["start_time"] <= s and e <= h["end_time"] and (not location_id or not h["location_id"] or h["location_id"] == location_id)
               for h in hours):
        return False
    lo, hi = to_utc_iso(start), to_utc_iso(end)
    if agenda_api.busy(db, therapist_id, lo, hi) or agenda_api.unavailable(db, therapist_id, start, end):
        return False
    rows = db.query("SELECT id, status, created_at FROM booking_requests WHERE practice_id = ? AND therapist_id = ? AND starts_at < ?"
                    " AND ends_at > ? AND status IN ('unverified', 'requested') AND id != ?",
                    (current_practice_id(), therapist_id, hi, lo, exclude_request or 0))
    fresh = datetime.now(TZ) - timedelta(minutes=CODE_MINUTES)
    return not any(r["status"] == "requested" or from_iso(r["created_at"]) >= fresh for r in rows)


# --------------------------------------------------------------------------- Anfragen
def code_hash(secret: bytes, request_id: int, code: str) -> str:
    return hmac.new(secret, f"booking:{request_id}:{code}".encode(), hashlib.sha256).hexdigest()


def create_request(db: Database, *, type_row, therapist_id: int, start: datetime, location_id: int | None, data: dict) -> int:
    end = start + timedelta(minutes=int(type_row["minutes"] or 30))
    rid = db.execute(
        "INSERT INTO booking_requests (practice_id, treatment_type_id, therapist_id, location_id, starts_at, ends_at, first_name, last_name,"
        " birthdate, phone, email, email_ok, lang, insurance, has_prescription, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), type_row["id"], therapist_id, location_id, to_utc_iso(start), to_utc_iso(end), data["first_name"],
         data["last_name"], data["birthdate"], data["phone"], data.get("email"), 1 if data.get("email_ok") and data.get("email") else 0,
         data["lang"], data.get("insurance"), 1 if data.get("has_prescription") else 0, data.get("note"), utcnow()))
    return rid


def set_code(db: Database, rid: int, digest: str) -> None:
    expires = (datetime.now(TZ) + timedelta(minutes=CODE_MINUTES)).isoformat()
    db.execute("UPDATE booking_requests SET code_hash = ?, code_expires_at = ? WHERE id = ? AND practice_id = ?",
               (digest, expires, rid, current_practice_id()))


def get(db: Database, rid: int):
    return db.one("SELECT r.*, u.name AS therapist_name, u.initials AS therapist_initials FROM booking_requests r"
                  " JOIN users u ON u.id = r.therapist_id WHERE r.id = ? AND r.practice_id = ?", (rid, current_practice_id()))


def verify(db: Database, secret: bytes, rid: int, code: str) -> str:
    """ok | wrong | expired | blocked"""
    r = get(db, rid)
    if r is None or r["status"] != "unverified":
        return "expired"
    if r["code_attempts"] >= MAX_CODE_ATTEMPTS:
        return "blocked"
    if not r["code_expires_at"] or from_iso(r["code_expires_at"]) < datetime.now(TZ):
        db.execute("UPDATE booking_requests SET status = 'expired' WHERE id = ?", (rid,))
        return "expired"
    if not hmac.compare_digest(r["code_hash"] or "", code_hash(secret, rid, code)):
        db.execute("UPDATE booking_requests SET code_attempts = code_attempts + 1 WHERE id = ?", (rid,))
        return "wrong"
    db.execute("UPDATE booking_requests SET status = 'requested', verified_at = ?, code_hash = NULL WHERE id = ? AND practice_id = ?",
               (utcnow(), rid, current_practice_id()))
    audit(db, "create", "booking_request", rid, None, {"source": "online"})
    return "ok"


def open_requests(db: Database):
    return db.query("SELECT r.*, u.name AS therapist_name FROM booking_requests r JOIN users u ON u.id = r.therapist_id"
                    " WHERE r.practice_id = ? AND r.status = 'requested' ORDER BY r.starts_at", (current_practice_id(),))


def recent_decided(db: Database, limit: int = 20):
    return db.query("SELECT r.*, u.name AS therapist_name, d.name AS decider_name FROM booking_requests r JOIN users u ON u.id = r.therapist_id"
                    " LEFT JOIN users d ON d.id = r.decided_by WHERE r.practice_id = ? AND r.status IN ('confirmed', 'declined')"
                    " ORDER BY r.decided_at DESC LIMIT ?", (current_practice_id(), limit))


def count_open(db: Database) -> int:
    return db.value("SELECT COUNT(*) FROM booking_requests WHERE practice_id = ? AND status = 'requested'", (current_practice_id(),), 0)


def existing_patient(db: Database, r):
    """Bekannte Person (gleicher Vorname, Name und Geburtsdatum) – nur wenn eindeutig."""
    rows = [p for p in core_patients.find_exact(db, r["first_name"], r["last_name"], r["birthdate"]) if not p["archived"]]
    return rows[0] if len(rows) == 1 else None


def _fold(text: str | None) -> str:
    """Für den Namensvergleich: klein, ohne Akzente und Bindestriche («Müller-Rey» ≈ «mueller rey» ≈ «muller rey»)."""
    s = (text or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    return " ".join(s.replace("-", " ").replace("'", " ").split())


def _distance(a: str, b: str) -> int:
    """Tippfehler-Abstand (Levenshtein: einfügen, löschen, ersetzen)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similar(a: str, b: str) -> bool:
    """Gleich bis auf einen Tippfehler (kurze Namen) bzw. zwei (ab 6 Zeichen), Umlaut-Schreibweise oder Doppelname."""
    if not a or not b:
        return False
    a2, b2 = a.replace("ae", "a").replace("oe", "o").replace("ue", "u"), b.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    if a2 == b2 or (min(len(a2), len(b2)) >= 4 and (a2 in b2.split() or b2 in a2.split())):
        return True
    return abs(len(a2) - len(b2)) <= 2 and _distance(a2, b2) <= (1 if max(len(a2), len(b2)) <= 5 else 2)


def likely_patients(db: Database, r, limit: int = 5) -> list[dict]:
    """Mögliche Treffer für eine Anfrage (Tippfehler, vertauschte Namen, gleiches Geburtsdatum oder gleiche Handynummer),
    beste zuerst: [{"p": Zeile, "reasons": ["name", "birthdate", "phone"], "exact": bool}]. Nur aktive Patient/innen der Praxis."""
    first, last = _fold(r["first_name"]), _fold(r["last_name"])
    phone = normalize_phone(r["phone"] or "") if r["phone"] else ""
    out = []
    for p in db.query("SELECT id, number, first_name, last_name, birthdate, phone FROM patients WHERE practice_id = ? AND archived = 0",
                      (current_practice_id(),)):
        pf, pl = _fold(p["first_name"]), _fold(p["last_name"])
        same_bd = bool(r["birthdate"]) and p["birthdate"] == r["birthdate"]
        same_phone = bool(phone) and bool(p["phone"]) and normalize_phone(p["phone"]) == phone
        name = (_similar(pl, last) and _similar(pf, first)) or (_similar(pl, first) and _similar(pf, last))
        last_only = _similar(pl, last)
        reasons = [k for k, ok in (("name", name), ("birthdate", same_bd), ("phone", same_phone)) if ok]
        if not (name or same_phone or (same_bd and last_only) or (same_bd and _similar(pf, first))):
            continue
        exact = pf == first and pl == last and same_bd
        score = (4 if exact else 0) + (2 if name else 0) + (2 if same_bd else 0) + (2 if same_phone else 0) + (1 if last_only else 0)
        out.append({"p": p, "reasons": reasons, "exact": exact, "score": score})
    out.sort(key=lambda x: (-x["score"], x["p"]["last_name"], x["p"]["first_name"]))
    return out[:limit]


def patient_for_choice(db: Database, value: str | None):
    """Gewählte Person aus dem Formular: ID oder «Name · Geburtsdatum · P00001» (nur aktive Patient/innen der Praxis)."""
    value = str(value or "").strip()
    m = re.search(r"(P\d{5,})\s*$", value)
    if m:
        p = db.one("SELECT * FROM patients WHERE practice_id = ? AND number = ?", (current_practice_id(), m.group(1)))
    elif value.isdigit():
        p = core_patients.get(db, int(value))
    else:
        p = None
    return p if p is not None and not p["archived"] else None


class _SlotTaken(Exception):
    """Zeit inzwischen belegt: die ganze Bestätigung wird zurückgenommen (auch eine eben angelegte neue Person)."""


def confirm(db: Database, r, *, patient_id: int | None = None, create_new: bool = False) -> tuple[int | None, int | None]:
    """Patient/in (bestehend oder neu) + Termin – alles oder nichts. (patient_id, appointment_id); (None, None), wenn die
    Anfrage nicht mehr offen ist oder die Zeit inzwischen belegt ist (dann bleibt alles wie vorher: keine neue Person,
    Anfrage weiter offen).
    patient_id: vom Empfang gewählte bestehende Person; create_new: bewusst neu anlegen; sonst nur bei eindeutigem Treffer
    (Vorname, Name, Geburtsdatum) die bestehende Person, ansonsten neu."""
    if r["status"] != "requested":
        return None, None
    types = {t["id"]: t for t in agenda_api.lookup_tables(db)["types"]}
    type_row = types.get(r["treatment_type_id"])
    start = from_iso(r["starts_at"]).astimezone(TZ)
    minutes = int(type_row["minutes"]) if type_row else int((from_iso(r["ends_at"]) - from_iso(r["starts_at"])).total_seconds() // 60)
    try:
        with db.tx():
            # Nochmals in der Transaktion prüfen: ein zweiter Klick (oder eine zweite Person am Empfang) bestätigt nicht doppelt.
            if db.value("SELECT status FROM booking_requests WHERE id = ? AND practice_id = ?", (r["id"], current_practice_id())) \
                    != "requested":
                return None, None
            # die eigene Anfrage hält die Zeit nicht mehr fest
            db.execute("UPDATE booking_requests SET status = 'expired' WHERE id = ? AND practice_id = ?", (r["id"], current_practice_id()))
            if patient_id is None and not create_new:
                found = existing_patient(db, r)
                patient_id = found["id"] if found else None
            if patient_id is None:
                patient_id = core_patients.create(db, {
                    "first_name": r["first_name"], "last_name": r["last_name"], "birthdate": r["birthdate"], "phone": r["phone"],
                    "email": r["email"], "lang": r["lang"], "law": r["insurance"] if r["insurance"] in INSURANCE else None,
                    "therapist_id": r["therapist_id"], "email_ok_at": r["verified_at"] if r["email_ok"] and r["email"] else None,
                    "reminder_channel": "sms"})
            aid = agenda_api.book(db, patient_id=patient_id, therapist_id=r["therapist_id"], start=start, minutes=minutes,
                                  treatment_type_id=r["treatment_type_id"], location_id=r["location_id"],
                                  note=f"Online-Anfrage #{r['id']}" + (f": {r['note']}" if r["note"] else ""))
            if aid is None:
                raise _SlotTaken()
            db.execute("UPDATE booking_requests SET status = 'confirmed', patient_id = ?, appointment_id = ?, decided_by = ?, decided_at = ?"
                       " WHERE id = ? AND practice_id = ?", (patient_id, aid, current_user_id(), utcnow(), r["id"], current_practice_id()))
            audit(db, "confirm", "booking_request", r["id"], patient_id, {"appointment": aid})
    except _SlotTaken:
        return None, None
    return patient_id, aid


def same_day(db: Database, r, patient_id: int | None) -> list:
    """Termine der gewählten / gefundenen Person am Tag der Anfrage (Hinweis vor dem Bestätigen)."""
    if not patient_id:
        return []
    return list(agenda_api.same_day(db, patient_id, from_iso(r["starts_at"])))


def decline(db: Database, r, reason: str | None) -> bool:
    if r["status"] != "requested":
        return False
    db.execute("UPDATE booking_requests SET status = 'declined', decline_reason = ?, decided_by = ?, decided_at = ? WHERE id = ?"
               " AND practice_id = ?", ((reason or "")[:300] or None, current_user_id(), utcnow(), r["id"], current_practice_id()))
    audit(db, "decline", "booking_request", r["id"], None, {})
    return True


def purge_old(db: Database, days: int = 90) -> None:
    """Abgelehnte/abgelaufene Anfragen nach 90 Tagen löschen (Datensparsamkeit)."""
    cutoff = to_utc_iso(datetime.now(TZ) - timedelta(days=days))
    db.execute("DELETE FROM booking_requests WHERE practice_id = ? AND status IN ('declined', 'expired', 'unverified')"
                      " AND created_at < ?", (current_practice_id(), cutoff))
