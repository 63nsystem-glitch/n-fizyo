"""Abwesenheiten, Schliessungen der Praxis und Feiertage: wann jemand (oder die ganze Praxis) nicht buchbar ist.

- Abwesenheit einer Therapeutin / eines Therapeuten: Ferien, Krankheit, Weiterbildung, Andere – ganze Tage oder ein Zeitfenster.
- Schliessung (user_id leer): Betriebsferien oder einzelner Schliesstag der ganzen Praxis.
- Feiertage: aus dem Kanton der Praxis (holidays.py), einzeln aus-/einschaltbar.

Online-Termine, «Lücke füllen» und Serien bieten in diesen Zeiten nichts an. Beim Buchen von Hand gibt es eine Warnung,
die das Team bewusst übergehen kann. Alles ist an die Praxis gebunden und wird protokolliert (ohne Bemerkungstext).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import TZ
from . import holidays

KINDS = ("vacation", "sick", "training", "other")      # Abwesenheit einer Person
ALL_KINDS = KINDS + ("closure",)                       # + Schliessung der Praxis
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_DAYS = 366


@dataclass(frozen=True)
class Off:
    """Ein nicht buchbarer Zeitraum an einem Tag (Ortszeit)."""
    kind: str                       # holiday | closure | vacation | sick | training | other
    start: datetime
    end: datetime
    whole_day: bool
    user_id: int | None = None      # None = alle (Feiertag, Schliessung)
    user_name: str | None = None
    holiday: str | None = None      # Code des Feiertags
    absence_id: int | None = None
    note: str | None = None

    @property
    def day(self) -> date:
        return self.start.date()

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start


def public_kind(kind: str, *, show_sick: bool) -> str:
    """Krankheit ist ein Gesundheitsdatum der Mitarbeitenden: ausser der Leitung sieht man nur «Abwesend»."""
    return "other" if kind == "sick" and not show_sick else kind


def describe(t, off: Off, *, name: bool = True, show_sick: bool = False) -> str:
    """Kurztext für Agenda-Band und Warnungen, z. B. «Ferien – A. Beispiel», «Karfreitag», «Praxis geschlossen – Inventur».
    Krankheit erscheint ausser für die Leitung (show_sick) neutral als «Abwesend»."""
    if off.kind == "holiday":
        text = t(f"agenda.hol.{off.holiday}")
    elif off.kind == "closure":
        text = t("agenda.abs.kind.closure") + (f" – {off.note}" if off.note else "")
    else:
        text = t(f"agenda.abs.kind.{public_kind(off.kind, show_sick=show_sick)}") + (f" – {off.user_name}" if name and off.user_name else "")
    if not off.whole_day:
        if off.end.date() == off.start.date():
            text += f" ({off.start.strftime('%H:%M')}–{off.end.strftime('%H:%M')})"
        else:
            text += f" ({t('agenda.abs.from_time', time=off.start.strftime('%H:%M'))})"
    return text


# --------------------------------------------------------------------------- Feiertage: Kanton und Auswahl
def practice_canton(db: Database) -> str:
    """Kanton aus den Angaben der Praxis (Einstellungen › Praxis), sonst BE."""
    value = db.value("SELECT canton FROM practices WHERE id = ?", (current_practice_id(),))
    value = str(value or "").strip().upper()
    return value if value in holidays.CANTONS else holidays.DEFAULT_CANTON


def holiday_setting(db: Database) -> dict:
    row = db.one("SELECT canton FROM holiday_settings WHERE practice_id = ?", (current_practice_id(),))
    own = row["canton"] if row and row["canton"] in holidays.CANTONS else None
    default = practice_canton(db)
    return {"canton": own, "default": default, "effective": own or default}


def canton(db: Database) -> str:
    return holiday_setting(db)["effective"]


def save_canton(db: Database, value: str | None) -> bool:
    value = (value or "").strip().upper() or None
    if value is not None and value not in holidays.CANTONS:
        return False
    db.execute("INSERT INTO holiday_settings (practice_id, canton, updated_at) VALUES (?,?,?) ON CONFLICT(practice_id)"
               " DO UPDATE SET canton = excluded.canton, updated_at = excluded.updated_at", (current_practice_id(), value, utcnow()))
    audit(db, "update", "holiday_settings", current_practice_id(), None, {"canton": value})
    return True


def overrides(db: Database, kanton: str) -> dict[str, bool]:
    return {r["code"]: bool(r["enabled"]) for r in db.query(
        "SELECT code, enabled FROM holiday_overrides WHERE practice_id = ? AND canton = ?", (current_practice_id(), kanton))}


def set_enabled(db: Database, kanton: str, code: str, enabled: bool) -> bool:
    if kanton not in holidays.CANTONS or code not in holidays.CODES:
        return False
    db.execute("INSERT INTO holiday_overrides (practice_id, canton, code, enabled) VALUES (?,?,?,?) ON CONFLICT(practice_id, canton, code)"
               " DO UPDATE SET enabled = excluded.enabled", (current_practice_id(), kanton, code, 1 if enabled else 0))
    audit(db, "update", "holiday_override", None, None, {"canton": kanton, "code": code, "enabled": bool(enabled)})
    return True


def year_list(db: Database, year: int) -> list[dict]:
    """Feiertage eines Jahres für die Einstellungen: {"h": Holiday, "enabled": bool}."""
    kanton = canton(db)
    own = overrides(db, kanton)
    return [{"h": h, "enabled": own.get(h.code, h.default_on)} for h in holidays.for_canton(kanton, year)]


def active_holidays(db: Database, first: date, last: date) -> list[holidays.Holiday]:
    kanton = canton(db)
    own = overrides(db, kanton)
    return [h for h in holidays.between(kanton, first, last) if own.get(h.code, h.default_on)]


# --------------------------------------------------------------------------- Abwesenheiten und Schliessungen
_SELECT = ("SELECT a.*, u.name AS user_name, u.initials AS user_initials, c.name AS created_by_name FROM absences a"
           " LEFT JOIN users u ON u.id = a.user_id LEFT JOIN users c ON c.id = a.created_by")


def get(db: Database, absence_id: int):
    return db.one(_SELECT + " WHERE a.id = ? AND a.practice_id = ?", (absence_id, current_practice_id()))


def listing(db: Database, *, user_id: int | None = None, since: date | None = None, until: date | None = None,
            closures: bool = True, only_closures: bool = False):
    sql = _SELECT + " WHERE a.practice_id = ?"
    params: list[Any] = [current_practice_id()]
    if only_closures:
        sql += " AND a.user_id IS NULL"
    elif user_id is not None:
        sql += " AND (a.user_id = ?" + (" OR a.user_id IS NULL)" if closures else ")")
        params.append(user_id)
    elif not closures:
        sql += " AND a.user_id IS NOT NULL"
    if since is not None:
        sql += " AND a.ends_on >= ?"
        params.append(since.isoformat())
    if until is not None:
        sql += " AND a.starts_on <= ?"
        params.append(until.isoformat())
    return db.query(sql + " ORDER BY a.starts_on, a.id", params)


def validate(values: dict, therapist_ids: set[int]) -> tuple[dict, list[str]]:
    """Formularwerte prüfen. Rückgabe: (saubere Werte, Fehlerfelder)."""
    errors: list[str] = []
    kind = str(values.get("kind") or "")
    if kind not in ALL_KINDS:
        errors.append("kind")
    user_id = None
    if kind != "closure":
        raw = str(values.get("user_id") or "")
        user_id = int(raw) if raw.isdigit() and len(raw) <= 9 else None
        if user_id not in therapist_ids:
            errors.append("user_id")
    try:
        first = date.fromisoformat(str(values.get("starts_on") or ""))
        last = date.fromisoformat(str(values.get("ends_on") or "") or first.isoformat())
        if last < first or (last - first).days > MAX_DAYS:
            raise ValueError
    except ValueError:
        first = last = None
        errors.append("dates")
    start_time = end_time = None
    part = values.get("part")
    if part == "time" or (part != "day" and (values.get("start_time") or values.get("end_time"))):
        start_time, end_time = str(values.get("start_time") or ""), str(values.get("end_time") or "")
        if not (HHMM.match(start_time) and HHMM.match(end_time) and start_time < end_time):
            errors.append("times")
    note = " ".join(str(values.get("note") or "").split())[:120] or None
    return ({"kind": kind, "user_id": user_id, "starts_on": first.isoformat() if first else None,
             "ends_on": last.isoformat() if last else None, "start_time": start_time, "end_time": end_time, "note": note}, errors)


def save(db: Database, clean: dict, absence_id: int | None = None) -> int:
    pid = current_practice_id()
    cols = ("user_id", "kind", "starts_on", "ends_on", "start_time", "end_time", "note")
    vals = tuple(clean[c] for c in cols)
    if absence_id:
        db.execute("UPDATE absences SET " + ", ".join(f"{c} = ?" for c in cols) + ", updated_at = ? WHERE id = ? AND practice_id = ?",
                   (*vals, utcnow(), absence_id, pid))
        action = "update"
    else:
        now = utcnow()
        absence_id = db.execute("INSERT INTO absences (practice_id, " + ", ".join(cols) + ", created_by, created_at, updated_at)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, *vals, current_user_id(), now, now))
        action = "create"
    audit(db, action, "absence", absence_id, None, {"kind": clean["kind"], "user": clean["user_id"], "from": clean["starts_on"],
                                                    "to": clean["ends_on"], "time": bool(clean["start_time"])})
    return absence_id


def delete(db: Database, absence_id: int) -> bool:
    row = get(db, absence_id)
    if row is None:
        return False
    db.execute("DELETE FROM absences WHERE id = ? AND practice_id = ?", (absence_id, current_practice_id()))
    audit(db, "delete", "absence", absence_id, None, {"kind": row["kind"], "user": row["user_id"], "from": row["starts_on"],
                                                      "to": row["ends_on"]})
    return True


# --------------------------------------------------------------------------- Nicht buchbare Zeiten
def _day_start(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=TZ)


def periods(db: Database, first: date, last: date, *, user_id: int | None = None, everyone: bool = False) -> list[Off]:
    """Nicht buchbare Zeiten von first bis last (je Tag): Feiertage und Schliessungen (alle) und die Abwesenheiten
    der Person user_id – bzw. aller Personen mit everyone=True."""
    out: list[Off] = []
    for h in active_holidays(db, first, last):
        start = _day_start(h.day) if not h.from_time else datetime.combine(h.day, time.fromisoformat(h.from_time), tzinfo=TZ)
        out.append(Off("holiday", start, _day_start(h.day + timedelta(days=1)), not h.from_time, holiday=h.code))
    sql = (_SELECT + " WHERE a.practice_id = ? AND a.starts_on <= ? AND a.ends_on >= ?")
    params: list[Any] = [current_practice_id(), last.isoformat(), first.isoformat()]
    if not everyone:
        sql += " AND (a.user_id IS NULL OR a.user_id = ?)"
        params.append(user_id or 0)
    for r in db.query(sql, params):
        d = max(first, date.fromisoformat(r["starts_on"]))
        end_day = min(last, date.fromisoformat(r["ends_on"]))
        while d <= end_day:
            if r["start_time"]:
                s = datetime.combine(d, time.fromisoformat(r["start_time"]), tzinfo=TZ)
                e = datetime.combine(d, time.fromisoformat(r["end_time"]), tzinfo=TZ)
                whole = False
            else:
                s, e, whole = _day_start(d), _day_start(d + timedelta(days=1)), True
            out.append(Off(r["kind"], s, e, whole, r["user_id"], r["user_name"], None, r["id"], r["note"]))
            d += timedelta(days=1)
    return sorted(out, key=lambda o: (o.start, o.kind != "holiday", o.user_id or 0))


def overlapping(db: Database, user_id: int | None, start: datetime, end: datetime) -> list[Off]:
    """Nicht buchbare Zeiten, die [start, end) berühren (Feiertag, Schliessung, Abwesenheit der Person)."""
    start, end = start.astimezone(TZ), end.astimezone(TZ)
    last = (end - timedelta(microseconds=1)).date()
    return [o for o in periods(db, start.date(), max(last, start.date()), user_id=user_id) if o.overlaps(start, end)]


def closures_ahead(db: Database, first: date, days: int = 60) -> list[dict]:
    """Kommende Schliessungen der Praxis (für Patient/innen: «Die Praxis ist geschlossen …»)."""
    rows = listing(db, since=first, until=first + timedelta(days=days), only_closures=True)
    return [{"from": max(first, date.fromisoformat(r["starts_on"])), "to": date.fromisoformat(r["ends_on"])}
            for r in rows if not r["start_time"]]
