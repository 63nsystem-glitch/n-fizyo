"""Öffentliche Schnittstelle der Agenda – nur das hier dürfen andere Module benutzen.

Termine lesen (für Diktat, Patienten-App, Berichte, Abrechnung, Kennzahlen) und Wünsche auf die Warteliste setzen.
Termine ändern andere Module NICHT direkt: Sie melden Ereignisse (z. B. «note.approved»), die Agenda reagiert.
"""
from __future__ import annotations

from datetime import date

from ...core.db import Database
from ...core.timeutil import from_iso
from . import absences, repo

STATUSES = ("booked", "arrived", "done", "cancelled", "no_show", "blocked")


def get(db: Database, appointment_id: int):
    """Termin mit Patient-, Therapie-, Raum- und Behandlungsart-Namen (oder None)."""
    return repo.get(db, appointment_id)


def on_day(db: Database, day: date, therapist_id: int | None = None):
    return repo.on_day(db, day, therapist_id=therapist_id)


def for_patient(db: Database, patient_id: int, *, future_only: bool = False, limit: int = 100):
    return repo.for_patient(db, patient_id, future_only=future_only, limit=limit)


def next_for_patient(db: Database, patient_id: int):
    return repo.next_for_patient(db, patient_id)


def today_for_patient(db: Database, patient_id: int):
    return repo.today_for_patient(db, patient_id)


def session_number(db: Database, appointment) -> tuple[int, int] | None:
    """(Nummer der Sitzung, verordnete Sitzungen) – oder None ohne Verordnung."""
    return repo.session_number(db, appointment)


def count_for_prescription(db: Database, prescription_id: int, statuses: tuple[str, ...] = ("done",)) -> int:
    return repo.count_for_prescription(db, prescription_id, statuses)


def used_sessions(db: Database, prescription_id: int) -> int:
    """Verbrauchte Sitzungen einer Verordnung – EINE Regel für Agenda, Verordnungen im Blick und Berichte: schon in der
    bisherigen Software durchgeführt (sessions_before) + erledigt + angemeldet + vergangene, nicht abgesagte Termine."""
    return repo.used_sessions(db, prescription_id)


def rx_usage(db: Database, prescription_ids=None) -> dict[int, dict]:
    """Wie used_sessions, für viele Verordnungen in einer Abfrage: {id: {"sessions", "before", "used", "planned", "patient_id"}}.
    planned = künftige gebuchte Termine dieser Verordnung. prescription_ids=None: alle Verordnungen der Praxis."""
    return repo.rx_usage(db, prescription_ids)


def future_booked(db: Database, patient_ids=None) -> dict[int, list]:
    """Künftige gebuchte Termine je Patient/in (id, patient_id, prescription_id, starts_at), aufsteigend – eine Abfrage.
    patient_ids=None: alle Patient/innen der Praxis."""
    from ...core.db import utcnow
    from ...core.tenant import current_practice_id

    sql = ("SELECT id, patient_id, prescription_id, starts_at FROM appointments WHERE practice_id = ? AND status = 'booked'"
           " AND patient_id IS NOT NULL AND starts_at >= ?")
    params: list = [current_practice_id(), utcnow()]
    if patient_ids is not None:
        ids = sorted({int(i) for i in patient_ids})
        if not ids:
            return {}
        sql += " AND patient_id IN (" + ",".join("?" for _ in ids) + ")"
        params += ids
    out: dict[int, list] = {}
    for r in db.query(sql + " ORDER BY starts_at, id", params):
        out.setdefault(r["patient_id"], []).append(r)
    return out


def same_day(db: Database, patient_id: int, start, exclude_id: int | None = None) -> list:
    """Andere Termine der Person am selben Tag (gebucht/angemeldet/erledigt) – Hinweis «am selben Tag schon ein Termin»."""
    from ...core.tenant import current_practice_id
    from ...core.timeutil import TZ, day_bounds_utc

    lo, hi = day_bounds_utc(start.astimezone(TZ).date())
    return db.query("SELECT id, starts_at, ends_at, therapist_id, status FROM appointments WHERE practice_id = ? AND patient_id = ?"
                    " AND id != ? AND status IN ('booked', 'arrived', 'done') AND starts_at >= ? AND starts_at < ? ORDER BY starts_at",
                    (current_practice_id(), patient_id, exclude_id or 0, lo, hi))


def future_for_patient(db: Database, patient_id: int):
    """Künftige gebuchte Termine der Person (mit Namen) – z. B. bevor die Akte archiviert wird."""
    return [a for a in repo.for_patient(db, patient_id, future_only=True, limit=200) if a["status"] == "booked"]


def cancel_future(db: Database, patient_id: int, reason: str | None = None) -> list:
    """Alle künftigen gebuchten Termine der Person absagen (z. B. beim Archivieren). Gibt die abgesagten Termine zurück."""
    rows = future_for_patient(db, patient_id)
    for a in rows:
        repo.set_status(db, a["id"], "cancelled", reason)
    return rows


def link_prescription(db: Database, patient_id: int, prescription_id: int) -> int:
    """Datenübernahme: noch nicht verknüpfte Termine der Person mit ihrer aktiven Verordnung verbinden (siehe repo.link_unlinked)."""
    return repo.link_unlinked(db, patient_id, prescription_id)


def done_span(db: Database, patient_id: int, prescription_id: int | None = None) -> dict:
    """{"n", "first", "last"} der erledigten Termine (optional nur einer Verordnung) – für Berichte."""
    return repo.done_span(db, patient_id, prescription_id)


def between(db: Database, start_utc: str, end_utc: str, *, therapist_id: int | None = None, patients_only: bool = False):
    """Schlanke Terminliste für Kennzahlen (ohne Namen)."""
    return repo.simple_between(db, start_utc, end_utc, therapist_id=therapist_id, patients_only=patients_only)


def done_appointments(db: Database, patient_id: int | None = None, since_utc: str | None = None):
    """Erledigte Termine (id, patient_id, starts_at, ends_at, prescription_id, therapist_id, treatment_type_id, tariff_code,
    type_de/fr, type_minutes) – Grundlage für Rechnungen."""
    return repo.done_appointments(db, patient_id, since_utc)


def done_patients(db: Database):
    return repo.done_patients(db)


def available_minutes_per_week(db: Database, user_id: int) -> int:
    """Arbeitszeit pro Woche laut Einstellungen (Minuten) – für die Auslastung."""
    total = 0
    for wd in range(7):
        for h in repo.hours_on(db, user_id, wd):
            sh, sm = map(int, h["start_time"].split(":"))
            eh, em = map(int, h["end_time"].split(":"))
            total += (eh * 60 + em) - (sh * 60 + sm)
    return total


def add_wish(db: Database, patient_id: int, wish: str | None, preferred: str | None) -> int:
    """Terminwunsch aus der Patienten-App → Warteliste des Empfangs."""
    return repo.add_waitlist(db, patient_id=patient_id, wish=wish, preferred=preferred, source="app")


def start_of(appointment):
    return from_iso(appointment["starts_at"])


# --------------------------------------------------------------------------- Datenübernahme (Modul «dataimport»)
def lookup_tables(db: Database) -> dict:
    """Standorte und Behandlungsarten der Praxis (für die Zuordnung beim Import)."""
    from ...core.tenant import current_practice_id

    pid = current_practice_id()
    return {"locations": db.query("SELECT id, name FROM locations WHERE practice_id = ? AND active = 1", (pid,)),
            "types": db.query("SELECT id, name_de, name_fr, minutes FROM treatment_types WHERE practice_id = ? AND active = 1", (pid,))}


def exists_at(db: Database, patient_id: int, start_utc: str) -> bool:
    from ...core.tenant import current_practice_id

    return bool(db.value("SELECT 1 FROM appointments WHERE practice_id = ? AND patient_id = ? AND starts_at = ?",
                         (current_practice_id(), patient_id, start_utc)))


def import_appointment(db: Database, *, patient_id: int, therapist_id: int, start, minutes: int, status: str,
                       location_id: int | None = None, treatment_type_id: int | None = None, note: str | None = None,
                       billed_externally: bool = False) -> int:
    """Termin aus der alten Software übernehmen (Status wie geliefert). Die Therapeutin / der Therapeut kommt ins Behandlungsteam.
    billed_externally: erledigter Termin ist im alten Programm schon verrechnet → nicht unter Abrechnung › «Bereit»."""
    from ...core import access
    from ...core.tenant import current_practice_id
    from ...core.timeutil import to_utc_iso

    if status not in STATUSES or status == "blocked":
        status = "booked"
    # mit der aktiven Verordnung verknüpfen wie beim Buchen von Hand (vergangene nur, wenn nicht schon als «bisher» gezählt)
    pr = repo.rx_for_new_appointment(db, patient_id, to_utc_iso(start), imported=True) if status != "cancelled" else None
    aid = repo.create(db, therapist_id=therapist_id, start=start, minutes=max(5, min(minutes, 600)), patient_id=patient_id,
                      location_id=location_id, treatment_type_id=treatment_type_id, prescription_id=pr["id"] if pr else None,
                      status=status, note=note)
    if billed_externally and status == "done":
        db.execute("UPDATE appointments SET billed_externally = 1 WHERE id = ? AND practice_id = ?", (aid, current_practice_id()))
    access.add_to_team(db, patient_id, therapist_id, "appointment")
    return aid


# --------------------------------------------------------------------------- Online-Termine (Modul «booking»)
def working_hours(db: Database, user_id: int, weekday: int):
    """Arbeitszeiten (start_time, end_time, location_id) einer Person an einem Wochentag (0 = Montag)."""
    return repo.hours_on(db, user_id, weekday)


def busy(db: Database, therapist_id: int, start_utc: str, end_utc: str) -> list[tuple[str, str]]:
    """Belegte Zeiten (UTC) einer Person: Termine und Blocker, die das Fenster berühren."""
    from ...core.tenant import current_practice_id

    rows = db.query("SELECT starts_at, ends_at FROM appointments WHERE practice_id = ? AND therapist_id = ? AND starts_at < ?"
                    " AND ends_at > ? AND status IN ('booked', 'arrived', 'done', 'blocked')",
                    (current_practice_id(), therapist_id, end_utc, start_utc))
    return [(r["starts_at"], r["ends_at"]) for r in rows]


def off_times(db: Database, therapist_id: int, first: date, last: date) -> list[tuple]:
    """Nicht buchbare Zeiten (Ortszeit, [(start, ende)]) einer Person von first bis last: Feiertage des Kantons, Schliessungen
    der Praxis und eigene Abwesenheiten (Ferien, Krankheit, Weiterbildung, Andere)."""
    return [(o.start, o.end) for o in absences.periods(db, first, last, user_id=therapist_id)]


def unavailable(db: Database, therapist_id: int, start, end) -> bool:
    """Fällt [start, end) in einen Feiertag, eine Schliessung oder eine Abwesenheit der Person?"""
    return bool(absences.overlapping(db, therapist_id, start, end))


def closures_ahead(db: Database, first: date, days: int = 60) -> list[dict]:
    """Kommende ganztägige Schliessungen der Praxis: [{"from": date, "to": date}] – für Hinweise an Patient/innen."""
    return absences.closures_ahead(db, first, days)


def book(db: Database, *, patient_id: int, therapist_id: int, start, minutes: int, treatment_type_id: int | None,
         location_id: int | None, note: str | None) -> int | None:
    """Termin aus einer bestätigten Online-Anfrage. None, wenn die Zeit inzwischen belegt ist oder in eine Abwesenheit,
    Schliessung oder einen Feiertag fällt."""
    from datetime import timedelta

    from ...core import access
    from ...core.timeutil import to_utc_iso

    end = start + timedelta(minutes=minutes)
    if repo.conflicts(db, therapist_id=therapist_id, start_utc=to_utc_iso(start), end_utc=to_utc_iso(end), patient_id=patient_id):
        return None
    if absences.overlapping(db, therapist_id, start, end):
        return None
    pr = repo.rx_for_new_appointment(db, patient_id)          # wie beim Buchen von Hand: aktive Verordnung verknüpfen
    aid = repo.create(db, therapist_id=therapist_id, start=start, minutes=minutes, patient_id=patient_id, location_id=location_id,
                      treatment_type_id=treatment_type_id, prescription_id=pr["id"] if pr else None, note=note)
    access.add_to_team(db, patient_id, therapist_id, "appointment")
    return aid


def locations(db: Database):
    """Aktive Standorte (id, name) – z. B. für Geräte der MTT-Fläche."""
    return repo.locations(db)


def rooms(db: Database, location_id: int | None = None):
    """Aktive Räume (id, name, location_id, location_name) – z. B. für die Einrichtungs-Checkliste."""
    return repo.rooms(db, location_id)


def group_size(db: Database, appointment) -> int:
    """Anwesende einer Gruppensitzung: erledigte Termine derselben Therapeutin / desselben Therapeuten mit gleichem Beginn und
    gleicher Behandlungsart (jede Person hat in der Agenda ihren eigenen Termin). Mindestens 1. Für die Divisormethode."""
    from ...core.tenant import current_practice_id

    return max(1, int(db.value(
        "SELECT COUNT(*) FROM appointments WHERE practice_id = ? AND therapist_id = ? AND starts_at = ? AND status = 'done'"
        " AND patient_id IS NOT NULL AND COALESCE(treatment_type_id, 0) = COALESCE(?, 0)",
        (current_practice_id(), appointment["therapist_id"], appointment["starts_at"], appointment["treatment_type_id"]), 1) or 1))
