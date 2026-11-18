"""Lücke füllen: ein abgesagter Termin wird passenden Personen auf der Warteliste per SMS angeboten.

Regeln (ohne Datenbank, testbar):
- Wer eine bestimmte Therapeutin, einen Standort oder eine Behandlungsart wünscht, bekommt nur passende Lücken.
- Wunschzeit (Freitext wie «Mo/Mi morgens», «mardi après-midi») wird gelesen; passt sie nicht, steht die Person
  unter «passt vielleicht» und wird nicht vorausgewählt.
- Wer zur selben Zeit schon einen Termin hat, kommt nicht in Frage.
- Hinweise (keine Sperre): keine gültige Verordnung, Verordnung aufgebraucht, am selben Tag schon ein Termin.
- Wer zuerst bestätigt, bekommt den Termin («first confirm wins», atomar in der Datenbank).
- Fällt die Lücke in einen Feiertag, eine Schliessung der Praxis oder eine Abwesenheit der Therapeutin / des Therapeuten,
  wird sie nie angeboten (auch nicht mehr, wenn die Abwesenheit erst nach dem Versand eingetragen wurde).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.security import new_token, token_hash
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import TZ, from_iso, to_utc_iso
from . import absences, repo

# --------------------------------------------------------------------------- Wunschzeit lesen
_DAYS = {
    0: ("mo", "mon", "montag", "montags", "lu", "lun", "lundi", "lunedi", "lunedì", "pzt", "pazartesi"),
    1: ("di", "die", "dienstag", "dienstags", "mar", "mardi", "martedi", "martedì", "sal", "salı", "sali"),
    2: ("mi", "mit", "mittwoch", "mittwochs", "me", "mer", "mercredi", "mercoledi", "mercoledì", "çar", "çarşamba", "carsamba"),
    3: ("do", "don", "donnerstag", "donnerstags", "je", "jeu", "jeudi", "giovedi", "giovedì", "per", "perşembe", "persembe"),
    4: ("fr", "fre", "freitag", "freitags", "ve", "ven", "vendredi", "venerdi", "venerdì", "cum", "cuma"),
    5: ("sa", "sam", "samstag", "samstags", "samedi", "sabato", "cmt", "cumartesi"),
}
_DAY_OF = {w: d for d, words in _DAYS.items() for w in words}
# Tageszeiten (Ortszeit, Minuten ab Mitternacht). Längere Wörter zuerst («nachmittag» enthält «mittag»).
_PARTS = (
    (("nachmittag", "après-midi", "apres-midi", "pomeriggio", "öğleden sonra", "ogleden sonra"), "afternoon", (12 * 60, 18 * 60)),
    (("vormittag", "morgen", "früh", "frueh", "matin", "mattina", "sabah"), "morning", (6 * 60, 12 * 60)),
    (("mittag", "midi", "mezzogiorno", "öğle", "ogle"), "noon", (11 * 60 + 30, 14 * 60)),
    (("abend", "soir", "sera", "akşam", "aksam"), "evening", (17 * 60, 21 * 60)),
)
_FLEX = ("egal", "immer", "jederzeit", "flexibel", "spontan", "kurzfristig", "n'importe", "flexible", "toujours", "sempre",
         "qualsiasi", "her zaman", "fark etmez", "farketmez", "any", "anytime")


@dataclass(frozen=True)
class Wish:
    days: frozenset[int] = frozenset()
    parts: tuple[tuple[str, tuple[int, int]], ...] = ()
    flexible: bool = False

    @property
    def empty(self) -> bool:
        return not self.days and not self.parts and not self.flexible


def parse_wish(text: str | None) -> Wish:
    """Wunschzeit aus Freitext. Unbekanntes wird ignoriert (dann zählt die Wunschzeit nicht)."""
    s = (text or "").lower()
    if not s.strip():
        return Wish()
    days: set[int] = set()
    for token in re.findall(r"[a-zçğıöşüàâéèêîôûì']+", s):
        token = token.strip("'")
        if token in _DAY_OF:
            days.add(_DAY_OF[token])
    # Bereiche wie «Mo–Mi» / «lu-me»
    for a, b in re.findall(r"\b([a-zçğıöşü]{2,3})\s*[-–]\s*([a-zçğıöşü]{2,3})\b", s):
        if a in _DAY_OF and b in _DAY_OF and _DAY_OF[a] <= _DAY_OF[b]:
            days.update(range(_DAY_OF[a], _DAY_OF[b] + 1))
    parts = []
    rest = s
    for words, name, span in _PARTS:
        if any(w in rest for w in words):
            parts.append((name, span))
            for w in words:
                rest = rest.replace(w, " ")
    return Wish(frozenset(days), tuple(parts), any(w in s for w in _FLEX))


# --------------------------------------------------------------------------- Passt die Person zur Lücke?
@dataclass
class Slot:
    start: datetime          # Ortszeit
    end: datetime
    therapist_id: int
    location_id: int | None = None
    treatment_type_id: int | None = None


@dataclass
class Candidate:
    entry: Any                                   # Zeile der Warteliste (mit Patientendaten)
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fits: bool = True                            # Wunschzeit passt (oder ist offen)
    waiting_days: int = 0


@dataclass
class Facts:
    """Was die Datenbank zu einer Person weiss (für die Regeln)."""
    overlaps: bool = False                       # hat zur selben Zeit schon einen Termin
    same_day: bool = False
    rx: str = "ok"                               # ok | none | used | n/a (nicht KVG oder ohne Patient)
    archived: bool = False


def evaluate(slot: Slot, entry, facts: Facts, now: datetime) -> Candidate | None:
    """None = kommt nicht in Frage. Sonst Kandidat mit Punkten, Gründen und Hinweisen."""
    if facts.archived or facts.overlaps:
        return None
    if entry["therapist_id"] and entry["therapist_id"] != slot.therapist_id:
        return None
    if entry["location_id"] and slot.location_id and entry["location_id"] != slot.location_id:
        return None
    if entry["treatment_type_id"] and slot.treatment_type_id and entry["treatment_type_id"] != slot.treatment_type_id:
        return None
    c = Candidate(entry)
    created = from_iso(entry["created_at"])
    c.waiting_days = max(0, (now - created).days)
    if entry["therapist_id"]:
        c.score += 3
        c.reasons.append("therapist")
    if entry["location_id"]:
        c.score += 1
        c.reasons.append("location")
    if entry["treatment_type_id"]:
        c.score += 1
        c.reasons.append("type")
    wish = parse_wish(entry["preferred"])
    if wish.flexible:
        c.score += 2
        c.reasons.append("flexible")
    if wish.days:
        if slot.start.weekday() in wish.days:
            c.score += 2
            c.reasons.append("day")
        elif not wish.flexible:
            c.fits = False
            c.warnings.append("day_no")
    if wish.parts:
        m0 = slot.start.hour * 60 + slot.start.minute
        m1 = slot.end.hour * 60 + slot.end.minute
        if any(a <= m0 and m1 <= b + 30 for _, (a, b) in wish.parts):
            c.score += 2
            c.reasons.append("daypart")
        elif not wish.flexible:
            c.fits = False
            c.warnings.append("daypart_no")
    if "needs_interpreter" in entry.keys() and entry["needs_interpreter"]:
        c.warnings.append("interpreter")             # kein Ausschluss – aber der Empfang muss die Übersetzung planen
    if facts.same_day:
        c.score -= 1
        c.warnings.append("same_day")
    if facts.rx in ("none", "used"):
        c.score -= 2
        c.warnings.append(f"rx_{facts.rx}")
    c.score += min(c.waiting_days // 7, 3)          # wer länger wartet, kommt etwas weiter nach oben
    return c


def rank(cands: list[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: (not c.fits, -c.score, -c.waiting_days, c.entry["id"]))


# --------------------------------------------------------------------------- Daten
OPEN_MINUTES_BEFORE = 30          # Angebot läuft 30 Minuten vor Beginn ab


def _utc(dt: datetime) -> str:
    return to_utc_iso(dt)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def off_time(db: Database, appt) -> list:
    """Feiertag, Schliessung oder Abwesenheit der Therapeutin / des Therapeuten zur Zeit der Lücke."""
    return absences.overlapping(db, appt["therapist_id"], from_iso(appt["starts_at"]), from_iso(appt["ends_at"]))


def gap_open(db: Database, appt) -> bool:
    """Ist die Zeit des abgesagten Termins noch frei (Therapeut/in), nicht abwesend / kein Feiertag und in der Zukunft?"""
    if appt["status"] != "cancelled" or not appt["patient_id"]:
        return False
    start = from_iso(appt["starts_at"])
    if start <= now_utc() + timedelta(minutes=OPEN_MINUTES_BEFORE):
        return False
    if repo.conflicts(db, therapist_id=appt["therapist_id"], start_utc=appt["starts_at"], end_utc=appt["ends_at"],
                      room_id=appt["room_id"], device_id=appt["device_id"], exclude_id=appt["id"]):
        return False
    return not off_time(db, appt)


def _facts(db: Database, entry, appt) -> Facts:
    pid = entry["patient_id"]
    if not pid:
        return Facts(rx="n/a")
    over = db.value("SELECT COUNT(*) FROM appointments WHERE practice_id = ? AND patient_id = ? AND id != ? AND status IN"
                    " ('booked', 'arrived', 'done') AND starts_at < ? AND ends_at > ?",
                    (current_practice_id(), pid, appt["id"], appt["ends_at"], appt["starts_at"]), 0)
    start = from_iso(appt["starts_at"]).astimezone(TZ)
    day0 = datetime(start.year, start.month, start.day, tzinfo=TZ)
    same = db.value("SELECT COUNT(*) FROM appointments WHERE practice_id = ? AND patient_id = ? AND status IN ('booked', 'arrived')"
                    " AND starts_at >= ? AND starts_at < ?", (current_practice_id(), pid, _utc(day0), _utc(day0 + timedelta(days=1))), 0)
    rx = "n/a"
    if (entry["law"] or "KVG").upper() == "KVG":
        pr = db.one("SELECT * FROM prescriptions WHERE practice_id = ? AND patient_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                    (current_practice_id(), pid))
        if pr is None:
            rx = "none"
        else:
            u = repo.rx_usage(db, [pr["id"]]).get(pr["id"], {"used": 0, "planned": 0})
            rx = "used" if pr["sessions"] and u["used"] + u["planned"] >= pr["sessions"] else "ok"
    return Facts(overlaps=bool(over), same_day=bool(same), rx=rx, archived=bool(entry["archived"]))


def waitlist_rows(db: Database):
    return db.query(
        "SELECT w.*, p.first_name, p.last_name, p.phone AS patient_phone, p.lang AS patient_lang, p.law, p.archived, p.needs_interpreter,"
        " u.name AS therapist_name FROM waitlist w LEFT JOIN patients p ON p.id = w.patient_id"
        " LEFT JOIN users u ON u.id = w.therapist_id WHERE w.practice_id = ? AND w.done = 0 ORDER BY w.id",
        (current_practice_id(),))


def slot_of(appt) -> Slot:
    return Slot(from_iso(appt["starts_at"]).astimezone(TZ), from_iso(appt["ends_at"]).astimezone(TZ),
                appt["therapist_id"], appt["location_id"], appt["treatment_type_id"])


def candidates(db: Database, appt) -> dict:
    """{"sms": [Candidate …] (Patient/in mit Handynummer), "call": [Zeile …] (nur telefonisch / ohne Akte)}."""
    slot = slot_of(appt)
    now = now_utc().astimezone(TZ)
    sms, call = [], []
    for w in waitlist_rows(db):
        if w["patient_id"] == appt["patient_id"]:
            continue                                    # wer abgesagt hat, bekommt die Lücke nicht angeboten
        c = evaluate(slot, w, _facts(db, w, appt), now)
        if c is None:
            continue
        if w["patient_id"] and (w["patient_phone"] or w["phone"]):
            sms.append(c)
        else:
            call.append(c)
    return {"sms": rank(sms), "call": rank(call)}


def current_offer(db: Database, appointment_id: int):
    expire_due(db)
    return db.one("SELECT o.*, b.starts_at AS booked_starts_at FROM slot_offers o LEFT JOIN appointments b ON b.id = o.booked_appointment_id"
                  " WHERE o.practice_id = ? AND o.appointment_id = ? ORDER BY o.id DESC LIMIT 1", (current_practice_id(), appointment_id))


def recipients(db: Database, offer_id: int):
    return db.query("SELECT r.*, p.first_name, p.last_name FROM slot_offer_recipients r JOIN patients p ON p.id = r.patient_id"
                    " WHERE r.practice_id = ? AND r.offer_id = ? ORDER BY r.id", (current_practice_id(), offer_id))


def expire_due(db: Database) -> None:
    db.execute("UPDATE slot_offers SET status = 'expired', closed_at = ? WHERE practice_id = ? AND status = 'open' AND expires_at <= ?",
               (utcnow(), current_practice_id(), _utc(now_utc())))


def create_offer(db: Database, appt) -> int:
    start = from_iso(appt["starts_at"])
    oid = db.execute(
        "INSERT INTO slot_offers (practice_id, appointment_id, therapist_id, location_id, room_id, device_id, treatment_type_id,"
        " starts_at, ends_at, expires_at, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), appt["id"], appt["therapist_id"], appt["location_id"], appt["room_id"], appt["device_id"],
         appt["treatment_type_id"], appt["starts_at"], appt["ends_at"], _utc(start - timedelta(minutes=OPEN_MINUTES_BEFORE)),
         current_user_id(), utcnow()))
    audit(db, "create", "slot_offer", oid, None, {"appointment": appt["id"]})
    return oid


def add_recipient(db: Database, offer_id: int, entry) -> str | None:
    """Gibt das Token für den SMS-Link zurück (nur hier im Klartext) – None, wenn schon angeschrieben."""
    if db.one("SELECT 1 FROM slot_offer_recipients WHERE offer_id = ? AND waitlist_id = ?", (offer_id, entry["id"])):
        return None
    token = new_token(24)
    db.execute("INSERT INTO slot_offer_recipients (practice_id, offer_id, waitlist_id, patient_id, phone, lang, token_hash, sent_at)"
               " VALUES (?,?,?,?,?,?,?,?)",
               (current_practice_id(), offer_id, entry["id"], entry["patient_id"], entry["patient_phone"] or entry["phone"],
                entry["patient_lang"] or "de", token_hash(token), utcnow()))
    audit(db, "create", "slot_offer_recipient", offer_id, entry["patient_id"])
    return token


def close_offer(db: Database, offer_id: int) -> list:
    """Angebot zurückziehen. Gibt die angeschriebenen Personen zurück, die noch nicht geantwortet haben."""
    rows = db.query("UPDATE slot_offers SET status = 'closed', closed_at = ? WHERE id = ? AND practice_id = ? AND status = 'open'"
                    " RETURNING id", (utcnow(), offer_id, current_practice_id()))
    if not rows:
        return []
    pending = db.query("SELECT * FROM slot_offer_recipients WHERE practice_id = ? AND offer_id = ? AND status = 'sent'",
                       (current_practice_id(), offer_id))
    db.execute("UPDATE slot_offer_recipients SET status = 'too_late', answered_at = ? WHERE practice_id = ? AND offer_id = ?"
               " AND status = 'sent'", (utcnow(), current_practice_id(), offer_id))
    audit(db, "update", "slot_offer", offer_id, None, {"status": "closed"})
    return pending


# --------------------------------------------------------------------------- Antwort über den SMS-Link (ohne Anmeldung)
def by_token(db: Database, token: str):
    """Absichtlich ohne Praxis-Filter: der Link selbst bestimmt die Praxis. Nur der Hash steht in der Datenbank."""
    return db.one("SELECT r.*, o.status AS offer_status, o.starts_at, o.ends_at, o.expires_at, o.therapist_id, o.location_id,"
                  " o.room_id, o.device_id, o.treatment_type_id, o.appointment_id, o.booked_appointment_id"
                  " FROM slot_offer_recipients r JOIN slot_offers o ON o.id = r.offer_id WHERE r.token_hash = ?", (token_hash(token),))


def accept(db: Database, rec) -> tuple[str, int | None, list]:
    """Wer zuerst bestätigt, bekommt den Termin. Ergebnis: ("booked"|"taken"|"expired"|"closed", Termin-Nr., andere Empfänger).
    Muss im practice_scope der Praxis des Angebots laufen."""
    with db.tx():
        expire_due(db)
        if rec["status"] == "accepted":
            return "booked", rec["booked_appointment_id"], []
        won = db.query("UPDATE slot_offers SET status = 'taken', closed_at = ? WHERE id = ? AND practice_id = ? AND status = 'open'"
                       " RETURNING id", (utcnow(), rec["offer_id"], current_practice_id()))
        if not won:
            status = db.value("SELECT status FROM slot_offers WHERE id = ?", (rec["offer_id"],))
            return ("expired" if status == "expired" else "closed" if status == "closed" else "taken"), None, []
        # Zeit inzwischen anders vergeben (z. B. am Telefon)? Dann ist das Angebot erledigt.
        busy = repo.conflicts(db, therapist_id=rec["therapist_id"], start_utc=rec["starts_at"], end_utc=rec["ends_at"],
                              room_id=rec["room_id"], device_id=rec["device_id"], patient_id=rec["patient_id"],
                              exclude_id=rec["appointment_id"])
        busy = busy or absences.overlapping(db, rec["therapist_id"], from_iso(rec["starts_at"]), from_iso(rec["ends_at"]))
        if busy:
            db.execute("UPDATE slot_offers SET status = 'closed' WHERE id = ?", (rec["offer_id"],))
            others = _others_too_late(db, rec)
            db.execute("UPDATE slot_offer_recipients SET status = 'too_late', answered_at = ? WHERE id = ?", (utcnow(), rec["id"]))
            return "taken", None, others
        start, end = from_iso(rec["starts_at"]), from_iso(rec["ends_at"])
        pr = db.one("SELECT id FROM prescriptions WHERE practice_id = ? AND patient_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                    (current_practice_id(), rec["patient_id"]))
        aid = repo.create(db, therapist_id=rec["therapist_id"], start=start.astimezone(TZ),
                          minutes=int((end - start).total_seconds() // 60), patient_id=rec["patient_id"], location_id=rec["location_id"],
                          room_id=rec["room_id"], device_id=rec["device_id"], treatment_type_id=rec["treatment_type_id"],
                          prescription_id=pr["id"] if pr else None, note="SMS-Angebot (Warteliste)")
        db.execute("UPDATE slot_offers SET booked_appointment_id = ? WHERE id = ?", (aid, rec["offer_id"]))
        db.execute("UPDATE slot_offer_recipients SET status = 'accepted', answered_at = ? WHERE id = ?", (utcnow(), rec["id"]))
        repo.waitlist_done(db, rec["waitlist_id"])
        others = _others_too_late(db, rec)
        audit(db, "update", "slot_offer", rec["offer_id"], rec["patient_id"], {"status": "taken", "appointment": aid, "by": "patient"})
        return "booked", aid, others


def _others_too_late(db: Database, rec) -> list:
    others = db.query("SELECT * FROM slot_offer_recipients WHERE offer_id = ? AND id != ? AND status = 'sent'", (rec["offer_id"], rec["id"]))
    db.execute("UPDATE slot_offer_recipients SET status = 'too_late', answered_at = ? WHERE offer_id = ? AND id != ? AND status = 'sent'",
               (utcnow(), rec["offer_id"], rec["id"]))
    return others


def decline(db: Database, rec) -> None:
    db.execute("UPDATE slot_offer_recipients SET status = 'declined', answered_at = ? WHERE id = ? AND status = 'sent'",
               (utcnow(), rec["id"]))
    audit(db, "update", "slot_offer_recipient", rec["offer_id"], rec["patient_id"], {"status": "declined", "by": "patient"})


def open_gaps(db: Database, days: int = 14) -> list[dict]:
    """Abgesagte Termine der nächsten Tage, deren Zeit noch frei ist – mit Stand des Angebots."""
    expire_due(db)
    now = now_utc()
    rows = db.query(repo._SELECT + " WHERE a.practice_id = ? AND a.status = 'cancelled' AND a.patient_id IS NOT NULL"
                    " AND a.starts_at > ? AND a.starts_at < ? ORDER BY a.starts_at",
                    (current_practice_id(), _utc(now + timedelta(minutes=OPEN_MINUTES_BEFORE)), _utc(now + timedelta(days=days))))
    out = []
    for a in rows:
        offer = db.one("SELECT * FROM slot_offers WHERE practice_id = ? AND appointment_id = ? ORDER BY id DESC LIMIT 1",
                       (current_practice_id(), a["id"]))
        if offer is not None and offer["status"] == "taken":
            continue
        if not gap_open(db, a):
            continue
        c = candidates(db, a)
        out.append({"a": a, "start": from_iso(a["starts_at"]), "offer": offer, "n_fit": sum(1 for x in c["sms"] if x.fits),
                    "n_all": len(c["sms"]) + len(c["call"])})
    return out
