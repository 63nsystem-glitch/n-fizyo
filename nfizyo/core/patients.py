"""Kern: Stammdaten der Patientinnen und Patienten, Versicherer, Zuweisende, Verordnungen.

Alle Module nutzen diese Funktionen (statt eigene Abfragen auf fremde Tabellen).
Jede Abfrage ist an die aktuelle Praxis gebunden."""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from . import access
from .audit import audit
from .db import Database, utcnow
from .tenant import current_practice_id, current_user_id
from .timeutil import today as zurich_today

PATIENT_FIELDS = (
    "first_name", "last_name", "birthdate", "sex", "lang", "spoken_lang", "needs_interpreter", "phone", "email",
    "address", "zip", "city", "insurer_id", "law", "insured_number", "ahv_number", "case_number", "accident_date", "employer",
    "referrer_id", "therapist_id", "goal", "admin_note", "email_ok_at", "reminder_channel",
)
REMINDER_CHANNELS = ("sms", "email", "both", "none")


def email_allowed(p) -> bool:
    """E-Mail an die Patientin / den Patienten nur mit Adresse und ausdrücklichem Einverständnis (unverschlüsselt)."""
    return bool(p and p["email"] and p["email_ok_at"])


def clean_ahv(value: str | None) -> str | None:
    """AHV-Nummer (AHVN13): 756.1234.5678.97 → 7561234567897. Prüfziffer nach EAN-13. Ungültig → ValueError."""
    if not value or not value.strip():
        return None
    digits = re.sub(r"[\s.]", "", value)
    if not re.fullmatch(r"756\d{10}", digits):
        raise ValueError("ahv")
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits[:12]))
    if (10 - total % 10) % 10 != int(digits[12]):
        raise ValueError("ahv")
    return digits


def format_ahv(value: str | None) -> str:
    d = value or ""
    return f"{d[:3]}.{d[3:7]}.{d[7:11]}.{d[11:]}" if len(d) == 13 else d


def split_street(address: str | None) -> tuple[str | None, str | None]:
    """«Bahnhofstrasse 12a» → («Bahnhofstrasse», «12a»). Nur die erste Zeile; ohne Nummer → (Strasse, None)."""
    line = (address or "").split("\n")[0].split(",")[0].strip()
    if not line:
        return None, None
    m = re.fullmatch(r"(.+?)\s+(\d+[A-Za-z]?(?:[-/]\d+[A-Za-z]?)?)", line)
    return (m.group(1), m.group(2)) if m else (line, None)


def age(birthdate: str | None, today: date | None = None) -> int | None:
    if not birthdate:
        return None
    b = date.fromisoformat(birthdate)
    t = today or zurich_today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def full_name(p) -> str:
    return f"{p['first_name']} {p['last_name']}" if p is not None else ""


def initials(p) -> str:
    return (p["first_name"][:1] + p["last_name"][:1]).upper() if p is not None else "?"


def next_number(db: Database) -> str:
    """Nächste Patientennummer (P00001 …) – nie eine schon einmal vergebene (auch nicht nach Zusammenführen/Löschen):
    über der grössten je vergebenen Nummer der Praxis (practices.patient_number_last, Migration core 012)."""
    pid = current_practice_id()
    last = db.value("SELECT patient_number_last FROM practices WHERE id = ?", (pid,), 0) or 0
    n = max(last, db.value("SELECT COUNT(*) FROM patients WHERE practice_id = ?", (pid,), 0)) + 1
    while db.value("SELECT 1 FROM patients WHERE practice_id = ? AND number = ?", (pid, f"P{n:05d}")):
        n += 1
    return f"P{n:05d}"


def next_id(db: Database) -> int:
    """Neue Patienten-ID über der grössten je vergebenen (IDs sind über alle Praxen eindeutig und werden nie wiederverwendet –
    sonst zeigten Protokoll und Zusammenführungen nach dem Löschen auf eine andere Person)."""
    return max(db.value("SELECT COALESCE(MAX(id), 0) FROM patients", (), 0),
               db.value("SELECT COALESCE(MAX(patient_id_last), 0) FROM practices", (), 0)) + 1


def create(db: Database, data: dict[str, Any]) -> int:
    pid = current_practice_id()
    now = utcnow()
    fields = {k: data.get(k) for k in PATIENT_FIELDS if k in data}
    fields.setdefault("lang", "de")
    fields["needs_interpreter"] = 1 if fields.get("needs_interpreter") else 0
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with db.tx():
        number = data.get("number") or next_number(db)
        patient_id = db.execute(
            f"INSERT INTO patients (id, practice_id, number, {cols}, created_at, updated_at) VALUES (?, ?, ?, {marks}, ?, ?)",
            (next_id(db), pid, number, *fields.values(), now, now),
        )
        if fields.get("therapist_id"):
            access.add_to_team(db, patient_id, int(fields["therapist_id"]), "main")
        audit(db, "create", "patient", patient_id, patient_id)
    return patient_id


def update(db: Database, patient_id: int, data: dict[str, Any]) -> None:
    pid = current_practice_id()
    fields = {k: data.get(k) for k in PATIENT_FIELDS if k in data}
    if "needs_interpreter" in fields:
        fields["needs_interpreter"] = 1 if fields["needs_interpreter"] else 0
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE patients SET {sets}, updated_at = ? WHERE id = ? AND practice_id = ?",
        (*fields.values(), utcnow(), patient_id, pid),
    )
    if fields.get("therapist_id"):
        access.add_to_team(db, patient_id, int(fields["therapist_id"]), "main")
    audit(db, "update", "patient", patient_id, patient_id, {"fields": sorted(fields)})


def get(db: Database, patient_id: int):
    return db.one(
        "SELECT p.*, i.name AS insurer_name, r.name AS referrer_name, u.name AS therapist_name"
        " FROM patients p LEFT JOIN insurers i ON i.id = p.insurer_id"
        " LEFT JOIN referrers r ON r.id = p.referrer_id LEFT JOIN users u ON u.id = p.therapist_id"
        " WHERE p.id = ? AND p.practice_id = ?",
        (patient_id, current_practice_id()),
    )


def search(db: Database, q: str = "", limit: int = 50, archived: bool = False):
    pid = current_practice_id()
    q = (q or "").strip()
    params: list[Any] = [pid, 1 if archived else 0]
    where = ""
    if q:
        like = f"%{q}%"
        where = (" AND (p.first_name LIKE ? OR p.last_name LIKE ? OR p.number LIKE ? OR p.phone LIKE ?"
                 " OR (p.first_name || ' ' || p.last_name) LIKE ? OR p.birthdate LIKE ?)")
        params += [like, like, like, like, like, like]
    params.append(limit)
    return db.query(
        "SELECT p.*, i.name AS insurer_name FROM patients p LEFT JOIN insurers i ON i.id = p.insurer_id"
        f" WHERE p.practice_id = ? AND p.archived = ?{where} ORDER BY p.last_name, p.first_name LIMIT ?",
        params,
    )


def set_archived(db: Database, patient_id: int, archived: bool) -> None:
    db.execute(
        "UPDATE patients SET archived = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
        (1 if archived else 0, utcnow(), patient_id, current_practice_id()),
    )
    audit(db, "archive" if archived else "unarchive", "patient", patient_id, patient_id)


def find_by_name_birthdate(db: Database, name: str | None, birthdate: date | None):
    """Für die Zuordnung eingehender Berichte. Liefert passende Patienten (bestes zuerst)."""
    pid = current_practice_id()
    if birthdate:
        rows = db.query(
            "SELECT * FROM patients WHERE practice_id = ? AND birthdate = ? AND archived = 0", (pid, birthdate.isoformat())
        )
        if name:
            parts = {p.lower() for p in name.replace(",", " ").split()}
            exact = [r for r in rows if r["last_name"].lower() in parts]
            if exact:
                return exact
        if rows:
            return rows
    if name:
        parts = [p for p in name.replace(",", " ").split() if len(p) > 1]
        if len(parts) >= 2:
            return db.query(
                "SELECT * FROM patients WHERE practice_id = ? AND archived = 0 AND"
                " ((first_name = ? AND last_name = ?) OR (first_name = ? AND last_name = ?))",
                (pid, parts[0], parts[-1], parts[-1], parts[0]),
            )
    return []


def find_exact(db: Database, first_name: str, last_name: str, birthdate: str | None = None):
    """Gleiche Person? Vorname + Name (Gross/Klein egal) und – wenn bekannt – Geburtsdatum. Für Datenübernahme und Duplikate."""
    sql = ("SELECT * FROM patients WHERE practice_id = ? AND lower(first_name) = lower(?) AND lower(last_name) = lower(?)")
    params: list = [current_practice_id(), first_name.strip(), last_name.strip()]
    if birthdate:
        sql += " AND birthdate = ?"
        params.append(birthdate)
    return db.query(sql, params)


# --------------------------------------------------------------------------- Verordnungen, Stammlisten
def prescriptions(db: Database, patient_id: int):
    return db.query(
        "SELECT pr.*, r.name AS referrer_name FROM prescriptions pr LEFT JOIN referrers r ON r.id = pr.referrer_id"
        " WHERE pr.practice_id = ? AND pr.patient_id = ? ORDER BY pr.id DESC",
        (current_practice_id(), patient_id),
    )


def active_prescription(db: Database, patient_id: int):
    return db.one(
        "SELECT * FROM prescriptions WHERE practice_id = ? AND patient_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (current_practice_id(), patient_id),
    )


PRESCRIPTION_KINDS = ("physio", "complex", "domicile", "lymph", "group")


def add_prescription(db: Database, patient_id: int, *, sessions: int, issued_on: str | None = None,
                     referrer_id: int | None = None, diagnosis: str | None = None, document_id: int | None = None,
                     kind: str = "physio", sessions_before: int = 0, status: str = "active") -> int:
    """Neue Verordnung. sessions_before: schon in der bisherigen Software durchgeführte Sitzungen (Datenübernahme)."""
    rid = db.execute(
        "INSERT INTO prescriptions (practice_id, patient_id, referrer_id, issued_on, sessions, kind, diagnosis, document_id,"
        " sessions_before, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, referrer_id, issued_on, sessions, kind if kind in PRESCRIPTION_KINDS else "physio",
         diagnosis, document_id, max(0, int(sessions_before or 0)), status if status in ("active", "used", "expired") else "active",
         utcnow()),
    )
    audit(db, "create", "prescription", rid, patient_id, {"sessions_before": sessions_before} if sessions_before else None)
    return rid


PRESCRIPTION_STATUSES = ("active", "used", "expired")


def prescription(db: Database, rid: int):
    return db.one("SELECT * FROM prescriptions WHERE id = ? AND practice_id = ?", (rid, current_practice_id()))


PRESCRIPTION_EDIT_FIELDS = ("issued_on", "sessions", "kind", "referrer_id", "diagnosis")


def update_prescription(db: Database, rid: int, values: dict) -> list[str]:
    """Verordnung korrigieren (z. B. Tippfehler bei Datum oder Sitzungen). Protokolliert alte und neue Werte. Gibt die geänderten
    Felder zurück."""
    row = prescription(db, rid)
    if row is None:
        return []
    clean = {k: values[k] for k in PRESCRIPTION_EDIT_FIELDS if k in values}
    if "kind" in clean and clean["kind"] not in PRESCRIPTION_KINDS:
        clean.pop("kind")
    changed = {k: v for k, v in clean.items() if row[k] != v}
    if not changed:
        return []
    db.execute("UPDATE prescriptions SET " + ", ".join(f"{k} = ?" for k in changed) + " WHERE id = ? AND practice_id = ?",
               (*changed.values(), rid, current_practice_id()))
    audit(db, "update", "prescription", rid, row["patient_id"],
          {"fields": sorted(changed), "before": {k: row[k] for k in changed if k != "diagnosis"}})
    return sorted(changed)


def set_prescription_status(db: Database, rid: int, status: str, *, reason: str | None = None) -> bool:
    """Verordnung beenden (used = aufgebraucht, expired = abgelaufen / nicht mehr gültig) oder wieder aktiv setzen."""
    row = prescription(db, rid)
    if row is None or status not in PRESCRIPTION_STATUSES or row["status"] == status:
        return False
    db.execute("UPDATE prescriptions SET status = ? WHERE id = ? AND practice_id = ?", (status, rid, current_practice_id()))
    audit(db, "status", "prescription", rid, row["patient_id"], {"from": row["status"], "to": status,
                                                                  **({"reason": reason[:200]} if reason else {})})
    return True


def prescription_exists(db: Database, patient_id: int, issued_on: str | None, sessions: int, referrer_id: int | None) -> bool:
    """Gibt es diese Verordnung schon (gleiche Person, Ausstellungsdatum, Sitzungen, Zuweiser/in)? – gegen doppelte Übernahme."""
    return bool(db.value(
        "SELECT 1 FROM prescriptions WHERE practice_id = ? AND patient_id = ? AND issued_on IS ? AND sessions = ? AND referrer_id IS ?",
        (current_practice_id(), patient_id, issued_on, sessions, referrer_id)))


def get_many(db: Database, patient_ids) -> dict:
    """Viele Patient/innen auf einmal (wie get(), mit Versicherer): {id: row}. Für Listen statt einer Abfrage je Zeile."""
    ids = sorted({int(i) for i in patient_ids})
    out: dict = {}
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        rows = db.query(
            "SELECT p.*, i.name AS insurer_name FROM patients p LEFT JOIN insurers i ON i.id = p.insurer_id"
            f" WHERE p.practice_id = ? AND p.id IN ({','.join('?' * len(part))})", [current_practice_id(), *part])
        out.update({r["id"]: r for r in rows})
    return out


def insurers(db: Database):
    return db.query("SELECT * FROM insurers WHERE practice_id = ? AND active = 1 ORDER BY name", (current_practice_id(),))


def insurer(db: Database, insurer_id: int | None):
    if not insurer_id:
        return None
    return db.one("SELECT * FROM insurers WHERE id = ? AND practice_id = ?", (insurer_id, current_practice_id()))


INSURER_FIELDS = ("name", "law", "gln", "tiers", "street", "zip", "city", "tp_group")
INSURER_TP_GROUPS = ("tarifsuisse", "hsk", "css", "other")   # Gruppe für den KVG-Taxpunktwert (leer = Wert der Praxis)


def all_insurers(db: Database):
    return db.query("SELECT * FROM insurers WHERE practice_id = ? ORDER BY active DESC, name", (current_practice_id(),))


def save_insurer(db: Database, values: dict, insurer_id: int | None = None) -> int:
    vals = tuple(values.get(k) for k in INSURER_FIELDS)
    if insurer_id:
        db.execute("UPDATE insurers SET " + ", ".join(f"{k} = ?" for k in INSURER_FIELDS) + " WHERE id = ? AND practice_id = ?",
                   (*vals, insurer_id, current_practice_id()))
        audit(db, "update", "insurer", insurer_id)
        return insurer_id
    iid = db.execute("INSERT INTO insurers (practice_id, " + ", ".join(INSURER_FIELDS) + ") VALUES ("
                     + ", ".join("?" for _ in range(len(INSURER_FIELDS) + 1)) + ")", (current_practice_id(), *vals))
    audit(db, "create", "insurer", iid)
    return iid


def toggle_insurer(db: Database, insurer_id: int) -> None:
    db.execute("UPDATE insurers SET active = 1 - active WHERE id = ? AND practice_id = ?", (insurer_id, current_practice_id()))


def insurer_by_name(db: Database, name: str):
    return db.one(
        "SELECT * FROM insurers WHERE practice_id = ? AND active = 1 AND lower(name) = lower(?)",
        (current_practice_id(), name),
    )


def referrers(db: Database):
    return db.query("SELECT * FROM referrers WHERE practice_id = ? AND active = 1 ORDER BY name", (current_practice_id(),))


REFERRER_FIELDS = ("name", "practice_name", "specialty", "address", "phone", "hin_email", "gln", "zsr", "lang")


def save_referrer(db: Database, values: dict, referrer_id: int | None = None) -> int:
    vals = tuple(values.get(k) for k in REFERRER_FIELDS)
    if referrer_id:
        db.execute("UPDATE referrers SET " + ", ".join(f"{k} = ?" for k in REFERRER_FIELDS) + " WHERE id = ? AND practice_id = ?",
                   (*vals, referrer_id, current_practice_id()))
        return referrer_id
    return db.execute("INSERT INTO referrers (practice_id, " + ", ".join(REFERRER_FIELDS) + ") VALUES ("
                      + ", ".join("?" for _ in range(len(REFERRER_FIELDS) + 1)) + ")", (current_practice_id(), *vals))


def toggle_referrer(db: Database, referrer_id: int) -> None:
    db.execute("UPDATE referrers SET active = 1 - active WHERE id = ? AND practice_id = ?", (referrer_id, current_practice_id()))


def therapists(db: Database):
    """Alle aktiven Personen mit Rolle Therapie (auch Leitung, die selbst behandelt)."""
    return db.query(
        "SELECT * FROM users WHERE practice_id = ? AND active = 1 AND (',' || roles || ',') LIKE '%,therapist,%' ORDER BY name",
        (current_practice_id(),),
    )


# --------------------------------------------------------------------------- Doppelte Einträge (nur Leitung)
# Jedes Modul meldet seine Tabellen mit Patientenbezug (ModuleContext.patient_tables): Zählen über das Ereignis
# «patient.linked», Zusammenführen über «patient.merge» – jedes Modul schreibt nur in eigene Tabellen.
MERGE_FILL = ("birthdate", "sex", "phone", "email", "address", "zip", "city", "insurer_id", "law", "insured_number", "ahv_number",
              "case_number", "accident_date", "employer", "referrer_id", "therapist_id", "spoken_lang")


class MergeError(Exception):
    pass


async def linked_counts(app, patient_id: int) -> dict[str, int]:
    """Einträge zu dieser Person je Tabelle (alle Module und Verordnungen). Leer = nichts verknüpft."""
    from .events import publish_strict
    from .keys import DB

    found: dict[str, int] = {}
    await publish_strict(app, "patient.linked", patient_id=patient_id, found=found)
    n = app[DB].value("SELECT COUNT(*) FROM prescriptions WHERE practice_id = ? AND patient_id = ?", (current_practice_id(), patient_id), 0)
    if n:
        found["prescriptions"] = n
    return found


def _drop_core_links(db: Database, patient_id: int) -> None:
    """Behandlungsteam und App-Sitzungen gehören zum Eintrag selbst (keine Daten)."""
    db.execute("DELETE FROM patient_team WHERE practice_id = ? AND patient_id = ?", (current_practice_id(), patient_id))
    db.execute("DELETE FROM sessions WHERE kind = 'patient' AND practice_id = ? AND patient_id = ?", (current_practice_id(), patient_id))


async def delete_empty(app, patient) -> bool:
    """Eintrag ohne verknüpfte Daten löschen (z. B. aus Versehen doppelt angelegt). Mit Daten → False (dann zusammenführen)."""
    from .keys import DB

    db = app[DB]
    if await linked_counts(app, patient["id"]):
        return False
    with db.tx():
        _drop_core_links(db, patient["id"])
        db.execute("DELETE FROM patients WHERE id = ? AND practice_id = ?", (patient["id"], current_practice_id()))
        audit(db, "delete", "patient", patient["id"], None, {"number": patient["number"], "reason": "empty_duplicate"})
    return True


def merge_fill(keep, drop) -> dict:
    """Felder, die im behaltenen Eintrag leer sind und vom Doppel-Eintrag übernommen werden."""
    return {k: drop[k] for k in MERGE_FILL if not keep[k] and drop[k]}


async def merge(app, drop, keep) -> dict[str, int]:
    """Doppel-Eintrag (drop) im behaltenen Eintrag (keep) aufgehen lassen – alles oder nichts, nicht rückgängig zu machen.
    Gibt die überführten Einträge je Tabelle zurück."""
    from .events import publish_strict
    from .keys import DB

    db = app[DB]
    if drop["id"] == keep["id"]:
        raise MergeError("same")
    moved = await linked_counts(app, drop["id"])
    fill = merge_fill(keep, drop)
    with db.tx():
        mid = db.execute("INSERT INTO patient_merges (practice_id, drop_id, keep_id, drop_number, created_by, created_at) VALUES (?,?,?,?,?,?)",
                         (current_practice_id(), drop["id"], keep["id"], drop["number"], current_user_id(), utcnow()))
        await publish_strict(app, "patient.merge", drop_id=drop["id"], keep_id=keep["id"])
        db.execute("UPDATE prescriptions SET patient_id = ? WHERE practice_id = ? AND patient_id = ?",
                   (keep["id"], current_practice_id(), drop["id"]))
        db.execute("INSERT OR IGNORE INTO patient_team (practice_id, patient_id, user_id, since, added_by, reason)"
                   " SELECT practice_id, ?, user_id, since, added_by, reason FROM patient_team WHERE practice_id = ? AND patient_id = ?",
                   (keep["id"], current_practice_id(), drop["id"]))
        _drop_core_links(db, drop["id"])
        if fill:
            db.execute("UPDATE patients SET " + ", ".join(f"{k} = ?" for k in fill) + ", updated_at = ? WHERE id = ? AND practice_id = ?",
                       (*fill.values(), utcnow(), keep["id"], current_practice_id()))
        left = await linked_counts(app, drop["id"])
        if left:
            raise MergeError(f"left: {sorted(left)}")
        db.execute("DELETE FROM patients WHERE id = ? AND practice_id = ?", (drop["id"], current_practice_id()))
        db.execute("UPDATE patient_merges SET finished_at = ?, summary = ? WHERE id = ?",
                   (utcnow(), json.dumps({"moved": moved, "filled": sorted(fill)}), mid))
        audit(db, "merge", "patient", keep["id"], keep["id"], {"drop": drop["id"], "drop_number": drop["number"], "moved": moved,
                                                               "filled": sorted(fill)})
    return moved


def likely_duplicates(db: Database, limit: int = 50) -> list[tuple]:
    """Mögliche Doppel-Einträge: gleiches Geburtsdatum und gleicher Name oder Vorname (Gross/Klein egal), oder gleicher Name
    und Vorname (eines ohne Geburtsdatum). Paare (älterer Eintrag, neuerer Eintrag).
    Gruppiert nach Geburtsdatum bzw. Name statt jede mit jeder zu vergleichen: 3000 Personen früher 2.7 s, jetzt Millisekunden."""
    rows = db.query("SELECT id, first_name, last_name, birthdate FROM patients WHERE practice_id = ?", (current_practice_id(),))
    by_birth: dict = {}
    by_name: dict = {}
    for r in rows:
        if r["birthdate"]:
            by_birth.setdefault(r["birthdate"], []).append(r)
        by_name.setdefault(((r["first_name"] or "").lower(), (r["last_name"] or "").lower()), []).append(r)

    def low(r):
        return (r["first_name"] or "").lower(), (r["last_name"] or "").lower()

    pairs: dict = {}
    for group in by_birth.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                (af, al), (bf, bl) = low(a), low(b)
                if al == bl or af == bf or (af == bl and al == bf):
                    x, y = (a, b) if a["id"] < b["id"] else (b, a)
                    pairs[(x["id"], y["id"])] = x
    for group in by_name.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if not a["birthdate"] or not b["birthdate"]:
                    x, y = (a, b) if a["id"] < b["id"] else (b, a)
                    pairs[(x["id"], y["id"])] = x
    ordered = sorted(pairs.items(), key=lambda kv: (kv[1]["last_name"] or "", kv[1]["first_name"] or "", kv[0]))[:limit]
    people = get_many(db, [i for (a, b), _ in ordered for i in (a, b)])
    return [(people[a], people[b]) for (a, b), _ in ordered if a in people and b in people]
