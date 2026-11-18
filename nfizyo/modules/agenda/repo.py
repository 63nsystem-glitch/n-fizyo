"""Datenzugriff Agenda (Zeiten in UTC gespeichert, Anzeige Europe/Zurich)."""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Any

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import TZ, day_bounds_utc, from_iso, local_dt, to_utc_iso
from . import absences

ACTIVE = ("booked", "arrived", "done", "blocked")


def locations(db: Database):
    return db.query("SELECT * FROM locations WHERE practice_id = ? AND active = 1 ORDER BY id", (current_practice_id(),))


def rooms(db: Database, location_id: int | None = None):
    sql = "SELECT r.*, l.name AS location_name FROM rooms r JOIN locations l ON l.id = r.location_id WHERE r.practice_id = ? AND r.active = 1"
    params: list[Any] = [current_practice_id()]
    if location_id:
        sql += " AND r.location_id = ?"
        params.append(location_id)
    return db.query(sql + " ORDER BY r.location_id, r.name", params)


def devices(db: Database, location_id: int | None = None):
    sql = "SELECT d.*, l.name AS location_name FROM devices d JOIN locations l ON l.id = d.location_id WHERE d.practice_id = ? AND d.active = 1"
    params: list[Any] = [current_practice_id()]
    if location_id:
        sql += " AND d.location_id = ?"
        params.append(location_id)
    return db.query(sql + " ORDER BY d.location_id, d.name", params)


def treatment_types(db: Database, include_inactive: bool = False):
    sql = "SELECT * FROM treatment_types WHERE practice_id = ?" + ("" if include_inactive else " AND active = 1")
    return db.query(sql + " ORDER BY active DESC, id", (current_practice_id(),))


def hours_on(db: Database, user_id: int, weekday: int, location_id: int | None = None):
    sql = "SELECT * FROM therapist_hours WHERE practice_id = ? AND user_id = ? AND weekday = ?"
    params: list[Any] = [current_practice_id(), user_id, weekday]
    if location_id:
        sql += " AND (location_id = ? OR location_id IS NULL)"
        params.append(location_id)
    return db.query(sql + " ORDER BY start_time", params)


def patient_choices(db: Database, only_ids_sql: tuple[str, list] | None = None):
    """Auswahlliste für Termine (Stammdaten aus dem Kern). only_ids_sql: z. B. nur eigene Patient/innen."""
    sql = "SELECT id, number, first_name, last_name, birthdate FROM patients WHERE practice_id = ? AND archived = 0"
    params: list[Any] = [current_practice_id()]
    if only_ids_sql:
        sql += f" AND id IN ({only_ids_sql[0]})"
        params += only_ids_sql[1]
    return db.query(sql + " ORDER BY last_name, first_name LIMIT 2000", params)


def patient_by_number(db: Database, number: str):
    return db.one("SELECT * FROM patients WHERE practice_id = ? AND number = ?", (current_practice_id(), number))


def treatment_type(db: Database, tid: int | None):
    if not tid:
        return None
    return db.one("SELECT * FROM treatment_types WHERE id = ? AND practice_id = ?", (tid, current_practice_id()))


_SELECT = (
    "SELECT a.*, p.first_name, p.last_name, p.spoken_lang, p.needs_interpreter, p.phone AS patient_phone,"
    " u.name AS therapist_name, u.initials AS therapist_initials, r.name AS room_name, d.name AS device_name,"
    " l.name AS location_name, tt.name_de AS type_de, tt.name_fr AS type_fr, tt.color AS type_color"
    " FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id JOIN users u ON u.id = a.therapist_id"
    " LEFT JOIN rooms r ON r.id = a.room_id LEFT JOIN devices d ON d.id = a.device_id"
    " LEFT JOIN locations l ON l.id = a.location_id LEFT JOIN treatment_types tt ON tt.id = a.treatment_type_id"
)


def get(db: Database, appointment_id: int):
    return db.one(_SELECT + " WHERE a.id = ? AND a.practice_id = ?", (appointment_id, current_practice_id()))


def between(db: Database, start_utc: str, end_utc: str, *, therapist_id: int | None = None,
            location_id: int | None = None, include_cancelled: bool = True):
    sql = _SELECT + " WHERE a.practice_id = ? AND a.starts_at < ? AND a.ends_at > ?"
    params: list[Any] = [current_practice_id(), end_utc, start_utc]
    if therapist_id:
        sql += " AND a.therapist_id = ?"
        params.append(therapist_id)
    if location_id:
        sql += " AND (a.location_id = ? OR a.location_id IS NULL)"
        params.append(location_id)
    if not include_cancelled:
        sql += " AND a.status NOT IN ('cancelled', 'no_show')"
    return db.query(sql + " ORDER BY a.starts_at, u.name", params)


def on_day(db: Database, d: date, **kw):
    start, end = day_bounds_utc(d)
    return between(db, start, end, **kw)


def for_patient(db: Database, patient_id: int, *, future_only: bool = False, limit: int = 100):
    sql = _SELECT + " WHERE a.practice_id = ? AND a.patient_id = ?"
    params: list[Any] = [current_practice_id(), patient_id]
    if future_only:
        sql += " AND a.starts_at >= ? AND a.status IN ('booked', 'arrived')"
        params.append(utcnow())
    sql += " ORDER BY a.starts_at" + ("" if future_only else " DESC") + " LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def next_for_patient(db: Database, patient_id: int):
    rows = for_patient(db, patient_id, future_only=True, limit=1)
    return rows[0] if rows else None


def today_for_patient(db: Database, patient_id: int, d: date | None = None):
    start, end = day_bounds_utc(d or datetime.now(TZ).date())
    return db.one(
        _SELECT + " WHERE a.practice_id = ? AND a.patient_id = ? AND a.starts_at >= ? AND a.starts_at < ?"
        " AND a.status IN ('booked', 'arrived', 'done') ORDER BY a.starts_at LIMIT 1",
        (current_practice_id(), patient_id, start, end),
    )


def session_number(db: Database, appt) -> tuple[int, int] | None:
    """(Nummer dieser Sitzung, Anzahl verordnet) für Termine mit Verordnung."""
    if appt is None or not appt["prescription_id"]:
        return None
    return session_numbers(db, [appt]).get(appt["id"])


def session_numbers(db: Database, appts) -> dict[int, tuple[int, int | None]]:
    """Wie session_number, aber für viele Termine auf einmal (Agenda, Terminlisten): {Termin-ID: (Nummer, verordnet)}.

    Eine einzige Abfrage für alle gezeigten Verordnungen (Index appointments_prescription). Nummer = schon in der bisherigen
    Software durchgeführte Sitzungen (sessions_before) + nicht abgesagte Termine dieser Verordnung bis und mit diesem Termin –
    dieselbe Zählung wie used_sessions() (Verordnungen im Blick)."""
    items = [a for a in appts if a["prescription_id"]]
    if not items:
        return {}
    ids = sorted({a["prescription_id"] for a in items})
    marks = ",".join("?" for _ in ids)
    totals: dict[int, tuple[int | None, int]] = {}
    starts: dict[int, list[str]] = {}
    for r in db.query(
        "SELECT pr.id, pr.sessions, pr.sessions_before, a.starts_at FROM prescriptions pr"
        " LEFT JOIN appointments a ON a.practice_id = pr.practice_id AND a.prescription_id = pr.id"
        "  AND a.status NOT IN ('cancelled', 'no_show')"
        f" WHERE pr.practice_id = ? AND pr.id IN ({marks}) ORDER BY pr.id, a.starts_at", (current_practice_id(), *ids)):
        totals[r["id"]] = (r["sessions"], r["sessions_before"] or 0)   # before: in der bisherigen Software
        if r["starts_at"]:
            starts.setdefault(r["id"], []).append(r["starts_at"])
    out: dict[int, tuple[int, int | None]] = {}
    for a in items:
        total, before = totals.get(a["prescription_id"], (None, 0))
        out[a["id"]] = (before + bisect_right(starts.get(a["prescription_id"], []), a["starts_at"]), total)
    return out


# --------------------------------------------------------------------------- Verbrauchte Sitzungen (eine Regel für alle)
# «Verbraucht» = schon in der bisherigen Software durchgeführt (sessions_before) + erledigt + angemeldet + vergangene Termine,
# die noch nicht als erledigt markiert, aber auch nicht abgesagt / nicht erschienen sind. Künftige gebuchte Termine sind
# «geplant». Dieselbe Regel nutzen Agenda (Sitzungsnummer, Serie), Lücken füllen, Verordnungen im Blick und Berichte.
USED_SQL = ("a.status IN ('done', 'arrived') OR (a.status = 'booked' AND a.starts_at < :now)")


def rx_usage(db: Database, prescription_ids=None, *, now: str | None = None) -> dict[int, dict]:
    """{Verordnung: {"sessions", "before", "used", "planned", "patient_id"}} in EINER Abfrage.
    prescription_ids=None: alle Verordnungen der Praxis (z. B. für «Verordnungen im Blick» über alle Patient/innen)."""
    params: dict[str, Any] = {"pid": current_practice_id(), "now": now or utcnow()}
    where = ""
    if prescription_ids is not None:
        ids = sorted({int(i) for i in prescription_ids if i})
        if not ids:
            return {}
        where = " AND pr.id IN (" + ",".join(f":r{i}" for i in range(len(ids))) + ")"
        params.update({f"r{i}": v for i, v in enumerate(ids)})
    rows = db.query(
        "SELECT pr.id, pr.patient_id, pr.sessions, COALESCE(pr.sessions_before, 0) AS before,"
        f" COUNT(CASE WHEN {USED_SQL} THEN 1 END) AS used,"
        " COUNT(CASE WHEN a.status = 'booked' AND a.starts_at >= :now THEN 1 END) AS planned"
        " FROM prescriptions pr LEFT JOIN appointments a ON a.practice_id = pr.practice_id AND a.prescription_id = pr.id"
        "  AND a.status IN ('done', 'arrived', 'booked')"
        f" WHERE pr.practice_id = :pid{where} GROUP BY pr.id", params)
    return {r["id"]: {"sessions": r["sessions"], "before": r["before"], "used": r["before"] + r["used"], "planned": r["planned"],
                      "patient_id": r["patient_id"]} for r in rows}


def used_sessions(db: Database, prescription_id: int, *, now: str | None = None) -> int:
    """Verbrauchte Sitzungen einer Verordnung (siehe USED_SQL, inkl. sessions_before)."""
    return rx_usage(db, [prescription_id], now=now).get(prescription_id, {}).get("used", 0)


def rx_for_new_appointment(db: Database, patient_id: int | None, start_utc: str | None = None, *, imported: bool = False):
    """Aktive Verordnung, mit der ein neuer Termin verknüpft wird (wie beim Buchen von Hand) – oder None.
    imported=True (Datenübernahme): vergangene Termine nur, wenn die Verordnung keine «bisher durchgeführten» Sitzungen
    (sessions_before) mitbringt – sonst wären dieselben Sitzungen doppelt gezählt."""
    if not patient_id:
        return None
    pr = db.one("SELECT * FROM prescriptions WHERE practice_id = ? AND patient_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (current_practice_id(), patient_id))
    if pr is None or not imported or start_utc is None:
        return pr
    if start_utc >= utcnow():
        return pr
    if pr["sessions_before"]:
        return None
    since = _issued_utc(pr)
    return pr if since is None or start_utc >= since else None


def _issued_utc(pr) -> str | None:
    try:
        return day_bounds_utc(date.fromisoformat(pr["issued_on"]))[0] if pr["issued_on"] else None
    except ValueError:
        return None


def link_unlinked(db: Database, patient_id: int, prescription_id: int) -> int:
    """Datenübernahme: noch nicht verknüpfte Termine der Person ab Ausstellungsdatum mit dieser (aktiven) Verordnung verbinden.
    Künftige immer; vergangene nur ohne sessions_before (die sind sonst schon als «bisher» gezählt). Gibt die Anzahl zurück."""
    pid = current_practice_id()
    pr = db.one("SELECT * FROM prescriptions WHERE id = ? AND practice_id = ? AND patient_id = ? AND status = 'active'",
                (prescription_id, pid, patient_id))
    if pr is None:
        return 0
    since = max(_issued_utc(pr) or "", utcnow()) if pr["sessions_before"] else (_issued_utc(pr) or "")
    rows = db.query("SELECT id FROM appointments WHERE practice_id = ? AND patient_id = ? AND prescription_id IS NULL"
                    " AND status IN ('booked', 'arrived', 'done') AND starts_at >= ?", (pid, patient_id, since))
    for r in rows:
        db.execute("UPDATE appointments SET prescription_id = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
                   (prescription_id, utcnow(), r["id"], pid))
    if rows:
        audit(db, "update", "prescription", prescription_id, patient_id, {"linked_appointments": len(rows)})
    return len(rows)


def beyond_prescription(session) -> bool:
    """Sitzung über der verordneten Anzahl («10/9») – gilt als «ohne Verordnung»."""
    return bool(session and session[1] and session[0] > session[1])


# --------------------------------------------------------------------------- Konflikte
def conflicts(db: Database, *, therapist_id: int, start_utc: str, end_utc: str, room_id: int | None = None,
              device_id: int | None = None, patient_id: int | None = None, exclude_id: int | None = None) -> list[dict]:
    out = []
    params: list[Any] = [current_practice_id(), end_utc, start_utc, exclude_id or 0]
    base = (_SELECT + " WHERE a.practice_id = ? AND a.starts_at < ? AND a.ends_at > ? AND a.id != ?"
            " AND a.status IN ('booked', 'arrived', 'done', 'blocked')")
    for kind, col, value in (("therapist", "a.therapist_id", therapist_id), ("room", "a.room_id", room_id),
                             ("device", "a.device_id", device_id), ("patient", "a.patient_id", patient_id)):
        if not value:
            continue
        for row in db.query(base + f" AND {col} = ?", params + [value]):
            out.append({"kind": kind, "appt": row})
    return out


def outside_hours(db: Database, therapist_id: int, start: datetime, end: datetime) -> bool:
    rows = db.query(
        "SELECT * FROM therapist_hours WHERE practice_id = ? AND user_id = ? AND weekday = ?",
        (current_practice_id(), therapist_id, start.weekday()),
    )
    if not rows:
        return False  # keine Arbeitszeiten erfasst → keine Warnung
    s, e = start.strftime("%H:%M"), end.strftime("%H:%M")
    return not any(r["start_time"] <= s and e <= r["end_time"] for r in rows)


def busy_times(db: Database, therapist_id: int, d: date) -> list[tuple[datetime, datetime]]:
    start, end = day_bounds_utc(d)
    rows = db.query("SELECT starts_at, ends_at FROM appointments WHERE practice_id = ? AND therapist_id = ? AND starts_at < ? AND ends_at > ?"
                    " AND status IN ('booked', 'arrived', 'done', 'blocked')", (current_practice_id(), therapist_id, end, start))
    return [(from_iso(r["starts_at"]), from_iso(r["ends_at"])) for r in rows]


def next_free(db: Database, therapist_id: int, minutes: int, after: datetime, days: int = 30, step: int = 15) -> datetime | None:
    """Erste freie Zeit einer Person ab «after»: in der Arbeitszeit, ohne Termine, Abwesenheiten, Schliessungen und Feiertage."""
    after = after.astimezone(TZ)
    first = after.date()
    offs = absences.periods(db, first, first + timedelta(days=days), user_id=therapist_id)
    for i in range(days + 1):
        d = first + timedelta(days=i)
        hours = hours_on(db, therapist_id, d.weekday())
        if not hours:
            continue
        busy = busy_times(db, therapist_id, d) + [(o.start, o.end) for o in offs if o.day == d]
        for h in hours:
            cursor, block_end = local_dt(d, h["start_time"]), local_dt(d, h["end_time"])
            while cursor + timedelta(minutes=minutes) <= block_end:
                end = cursor + timedelta(minutes=minutes)
                if cursor >= after and not any(b0 < end and b1 > cursor for b0, b1 in busy):
                    return cursor
                cursor += timedelta(minutes=step)
    return None


# --------------------------------------------------------------------------- Schreiben
def create(db: Database, *, therapist_id: int, start: datetime, minutes: int, patient_id: int | None = None,
           location_id: int | None = None, room_id: int | None = None, device_id: int | None = None,
           treatment_type_id: int | None = None, prescription_id: int | None = None, series_id: int | None = None,
           status: str = "booked", title: str | None = None, note: str | None = None) -> int:
    now = utcnow()
    end = start + timedelta(minutes=minutes)
    aid = db.execute(
        "INSERT INTO appointments (practice_id, patient_id, therapist_id, location_id, room_id, device_id, treatment_type_id,"
        " prescription_id, series_id, starts_at, ends_at, status, title, note, created_by, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, therapist_id, location_id, room_id, device_id, treatment_type_id,
         prescription_id, series_id, to_utc_iso(start), to_utc_iso(end), status, title, note, current_user_id(), now, now),
    )
    audit(db, "create", "appointment", aid, patient_id)
    return aid


def update(db: Database, appointment_id: int, **fields) -> None:
    allowed = {"therapist_id", "location_id", "room_id", "device_id", "treatment_type_id", "prescription_id",
               "starts_at", "ends_at", "note", "title", "patient_id"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    row = db.one("SELECT patient_id, starts_at FROM appointments WHERE id = ? AND practice_id = ?", (appointment_id, current_practice_id()))
    if row is not None and "starts_at" in fields and fields["starts_at"] != row["starts_at"]:
        sets += ", reminder_sent_at = NULL"       # verschoben: für die neue Zeit wieder erinnern (sonst merkt sich die Person die alte)
    db.execute(f"UPDATE appointments SET {sets}, updated_at = ? WHERE id = ? AND practice_id = ?",
               (*fields.values(), utcnow(), appointment_id, current_practice_id()))
    audit(db, "update", "appointment", appointment_id, row["patient_id"] if row else None, {"fields": sorted(fields)})


def set_status(db: Database, appointment_id: int, status: str, reason: str | None = None) -> None:
    row = db.one("SELECT patient_id FROM appointments WHERE id = ? AND practice_id = ?", (appointment_id, current_practice_id()))
    if row is None:
        return
    db.execute("UPDATE appointments SET status = ?, cancel_reason = COALESCE(?, cancel_reason), updated_at = ?"
               " WHERE id = ? AND practice_id = ?", (status, reason, utcnow(), appointment_id, current_practice_id()))
    audit(db, "update", "appointment", appointment_id, row["patient_id"], {"status": status})


def create_series(db: Database, patient_id: int, prescription_id: int | None) -> int:
    return db.execute(
        "INSERT INTO appointment_series (practice_id, patient_id, prescription_id, created_by, created_at) VALUES (?,?,?,?,?)",
        (current_practice_id(), patient_id, prescription_id, current_user_id(), utcnow()),
    )


def series_slots(start_day: date, weekdays: list[int], hhmm: str, count: int, minutes: int,
                 skip=None) -> list[tuple[datetime, datetime]] | tuple[list[tuple[datetime, datetime]], list[tuple[datetime, list]]]:
    """Termine einer Serie: ab start_day an den gewählten Wochentagen, count Mal.

    skip(start, end) -> Liste der Gründe (z. B. Feiertag, Abwesenheit); ist sie nicht leer, wird der Tag übersprungen und die
    Serie läuft weiter, bis count Termine beisammen sind. Mit skip ist das Ergebnis (Termine, übersprungene [(start, Gründe)])."""
    out: list[tuple[datetime, datetime]] = []
    skipped: list[tuple[datetime, list]] = []
    d = start_day
    guard = 0
    while len(out) < count and guard < 400:
        if d.weekday() in weekdays:
            s = local_dt(d, hhmm)
            e = s + timedelta(minutes=minutes)
            reasons = skip(s, e) if skip else None
            if reasons:
                skipped.append((s, reasons))
            else:
                out.append((s, e))
        d += timedelta(days=1)
        guard += 1
    return (out, skipped) if skip else out


# --------------------------------------------------------------------------- Warteliste
def waitlist(db: Database, include_done: bool = False):
    return db.query(
        "SELECT w.*, p.first_name, p.last_name, u.name AS therapist_name, l.name AS location_name"
        " FROM waitlist w LEFT JOIN patients p ON p.id = w.patient_id LEFT JOIN users u ON u.id = w.therapist_id"
        " LEFT JOIN locations l ON l.id = w.location_id WHERE w.practice_id = ?" + ("" if include_done else " AND w.done = 0")
        + " ORDER BY w.id",
        (current_practice_id(),),
    )


def add_waitlist(db: Database, **f) -> int:
    return db.execute(
        "INSERT INTO waitlist (practice_id, patient_id, name, phone, wish, preferred, therapist_id, location_id,"
        " treatment_type_id, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), f.get("patient_id"), f.get("name"), f.get("phone"), f.get("wish"), f.get("preferred"),
         f.get("therapist_id"), f.get("location_id"), f.get("treatment_type_id"), f.get("source", "reception"), utcnow()),
    )


def waitlist_done(db: Database, wid: int) -> None:
    db.execute("UPDATE waitlist SET done = 1 WHERE id = ? AND practice_id = ?", (wid, current_practice_id()))


# --------------------------------------------------------------------------- Erinnerungen
def due_reminders(db: Database, practice_id: int, hours: int = 24):
    now = datetime.now(TZ)
    return db.query(
        _SELECT + " WHERE a.practice_id = ? AND a.status = 'booked' AND a.patient_id IS NOT NULL AND a.reminder_sent_at IS NULL"
        " AND a.starts_at > ? AND a.starts_at <= ? AND (p.phone IS NOT NULL OR (p.email IS NOT NULL AND p.email_ok_at IS NOT NULL))"
        " AND p.reminder_channel != 'none' AND p.archived = 0",          # archivierte Personen: keine Erinnerungen
        (practice_id, to_utc_iso(now), to_utc_iso(now + timedelta(hours=hours))),
    )


def mark_reminded(db: Database, practice_id: int, appointment_id: int) -> None:
    db.execute("UPDATE appointments SET reminder_sent_at = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), appointment_id, practice_id))


def local(value: str) -> datetime:
    return from_iso(value)


# --------------------------------------------------------------------------- Zahlen für andere Module
def count_for_prescription(db: Database, prescription_id: int, statuses: tuple[str, ...] = ("done",)) -> int:
    marks = ",".join("?" for _ in statuses)
    return db.value(f"SELECT COUNT(*) FROM appointments WHERE practice_id = ? AND prescription_id = ? AND status IN ({marks})",
                    (current_practice_id(), prescription_id, *statuses), 0)


def done_span(db: Database, patient_id: int, prescription_id: int | None = None) -> dict:
    sql = ("SELECT COUNT(*) AS n, MIN(starts_at) AS first, MAX(starts_at) AS last FROM appointments"
           " WHERE practice_id = ? AND patient_id = ? AND status = 'done'")
    params: list[Any] = [current_practice_id(), patient_id]
    if prescription_id:
        sql += " AND prescription_id = ?"
        params.append(prescription_id)
    row = db.one(sql, params)
    return {"n": row["n"] if row else 0, "first": row["first"] if row else None, "last": row["last"] if row else None}


def simple_between(db: Database, start_utc: str, end_utc: str, *, therapist_id: int | None = None, patients_only: bool = False):
    """Schlanke Liste (ohne Namen) für Kennzahlen: id, patient_id, therapist_id, starts_at, ends_at, status, treatment_type_id."""
    sql = ("SELECT id, patient_id, therapist_id, prescription_id, treatment_type_id, starts_at, ends_at, status FROM appointments"
           " WHERE practice_id = ? AND starts_at >= ? AND starts_at < ?")
    params: list[Any] = [current_practice_id(), start_utc, end_utc]
    if therapist_id:
        sql += " AND therapist_id = ?"
        params.append(therapist_id)
    if patients_only:
        sql += " AND patient_id IS NOT NULL"
    return db.query(sql + " ORDER BY starts_at", params)


def done_appointments(db: Database, patient_id: int | None = None, since_utc: str | None = None):
    """Erledigte Termine mit Patient/in und Tarifziffer der Behandlungsart (für die Abrechnung)."""
    sql = ("SELECT a.id, a.patient_id, a.starts_at, a.ends_at, a.prescription_id, a.therapist_id, a.treatment_type_id, t.tariff_code,"
           " t.name_de AS type_de, t.name_fr AS type_fr, t.minutes AS type_minutes, a.billed_externally"
           " FROM appointments a LEFT JOIN treatment_types t ON t.id = a.treatment_type_id"
           " WHERE a.practice_id = ? AND a.status = 'done' AND a.patient_id IS NOT NULL")
    params: list[Any] = [current_practice_id()]
    if patient_id:
        sql += " AND a.patient_id = ?"
        params.append(patient_id)
    if since_utc:
        sql += " AND a.starts_at >= ?"
        params.append(since_utc)
    return db.query(sql + " ORDER BY a.starts_at", params)


def done_patients(db: Database):
    """Patient/innen mit erledigten Terminen: id, Anzahl, erster und letzter Termin."""
    return db.query("SELECT patient_id, COUNT(*) AS n, MIN(starts_at) AS first, MAX(starts_at) AS last FROM appointments"
                    " WHERE practice_id = ? AND status = 'done' AND patient_id IS NOT NULL GROUP BY patient_id",
                    (current_practice_id(),))


# --------------------------------------------------------------------------- Einstellungen (Leitung)
def all_locations(db: Database):
    pid = current_practice_id()
    out = []
    for loc in db.query("SELECT * FROM locations WHERE practice_id = ? ORDER BY active DESC, name", (pid,)):
        out.append({"l": loc,
                    "rooms": db.query("SELECT * FROM rooms WHERE location_id = ? AND practice_id = ? ORDER BY active DESC, name", (loc["id"], pid)),
                    "devices": db.query("SELECT * FROM devices WHERE location_id = ? AND practice_id = ? ORDER BY active DESC, name", (loc["id"], pid))})
    return out


def add_location(db: Database, name: str, address: str | None, phone: str | None) -> int:
    lid = db.execute("INSERT INTO locations (practice_id, name, address, phone) VALUES (?,?,?,?)", (current_practice_id(), name, address, phone))
    audit(db, "create", "location", lid)
    return lid


def add_room_or_device(db: Database, kind: str, location_id: int, name: str) -> bool:
    pid = current_practice_id()
    if kind not in ("room", "device") or not db.value("SELECT 1 FROM locations WHERE id = ? AND practice_id = ?", (location_id, pid)):
        return False
    table = "rooms" if kind == "room" else "devices"
    db.execute(f"INSERT INTO {table} (practice_id, location_id, name) VALUES (?,?,?)", (pid, location_id, name))
    return True


def toggle_active(db: Database, table: str, row_id: int) -> bool:
    if table not in ("locations", "rooms", "devices", "treatment_types"):
        return False
    db.execute(f"UPDATE {table} SET active = 1 - active WHERE id = ? AND practice_id = ?", (row_id, current_practice_id()))
    audit(db, "update", table.rstrip("s"), row_id, None, {"toggle": "active"})
    return True


def location(db: Database, location_id: int | None):
    if not location_id:
        return None
    return db.one("SELECT * FROM locations WHERE id = ? AND practice_id = ?", (location_id, current_practice_id()))


def update_location(db: Database, location_id: int, name: str, address: str | None, phone: str | None) -> bool:
    if location(db, location_id) is None or not name:
        return False
    db.execute("UPDATE locations SET name = ?, address = ?, phone = ? WHERE id = ? AND practice_id = ?",
               (name, address, phone, location_id, current_practice_id()))
    audit(db, "update", "location", location_id, None, {"fields": ["address", "name", "phone"]})
    return True


def rename(db: Database, table: str, row_id: int, name: str) -> bool:
    """Raum oder Gerät umbenennen (nur in der eigenen Praxis)."""
    if table not in ("rooms", "devices") or not name:
        return False
    pid = current_practice_id()
    if not db.value(f"SELECT 1 FROM {table} WHERE id = ? AND practice_id = ?", (row_id, pid)):
        return False
    db.execute(f"UPDATE {table} SET name = ? WHERE id = ? AND practice_id = ?", (name, row_id, pid))
    audit(db, "update", table.rstrip("s"), row_id, None, {"fields": ["name"]})
    return True


def save_treatment_type(db: Database, values: dict, type_id: int | None = None) -> None:
    cols = ("name_de", "name_fr", "minutes", "tariff_code", "needs_device", "color")
    vals = tuple(values[c] for c in cols)
    if type_id:
        db.execute("UPDATE treatment_types SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ? AND practice_id = ?",
                   (*vals, type_id, current_practice_id()))
    else:
        db.execute("INSERT INTO treatment_types (practice_id, " + ", ".join(cols) + ") VALUES (?,?,?,?,?,?,?)",
                   (current_practice_id(), *vals))


def all_hours(db: Database):
    return db.query("SELECT h.*, u.name AS user_name, l.name AS location_name FROM therapist_hours h JOIN users u ON u.id = h.user_id"
                    " LEFT JOIN locations l ON l.id = h.location_id WHERE h.practice_id = ? ORDER BY u.name, h.weekday, h.start_time",
                    (current_practice_id(),))


def add_hours(db: Database, user_id: int, location_id: int | None, weekdays: list[int], start: str, end: str) -> None:
    pid = current_practice_id()
    if location_id and not db.value("SELECT 1 FROM locations WHERE id = ? AND practice_id = ?", (location_id, pid)):
        location_id = None
    for d in weekdays:
        db.execute("INSERT INTO therapist_hours (practice_id, user_id, location_id, weekday, start_time, end_time) VALUES (?,?,?,?,?,?)",
                   (pid, user_id, location_id, d, start, end))


def remove_hours(db: Database, hours_id: int) -> None:
    db.execute("DELETE FROM therapist_hours WHERE id = ? AND practice_id = ?", (hours_id, current_practice_id()))
