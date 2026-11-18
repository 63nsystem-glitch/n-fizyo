"""Notizen: Entwurf → Freigabe (gesperrt) → Nachträge als neue Versionen."""
from __future__ import annotations

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import today

SECTIONS = ("befund", "behandlung", "beurteilung", "plan")


class NoteLocked(Exception):
    """Freigegebene Notiz – nur Nachtrag möglich."""


def _latest_sql() -> str:
    return (
        "SELECT n.*, v.version, v.befund, v.behandlung, v.beurteilung, v.plan, v.reason, v.created_at AS version_at,"
        " u.name AS author_name, ua.name AS approver_name"
        " FROM notes n JOIN note_versions v ON v.note_id = n.id"
        " AND v.version = (SELECT MAX(version) FROM note_versions WHERE note_id = n.id)"
        " JOIN users u ON u.id = n.author_id LEFT JOIN users ua ON ua.id = n.approved_by"
    )


def get(db: Database, note_id: int):
    return db.one(_latest_sql() + " WHERE n.id = ? AND n.practice_id = ?", (note_id, current_practice_id()))


def for_patient(db: Database, patient_id: int, limit: int = 200):
    return db.query(_latest_sql() + " WHERE n.practice_id = ? AND n.patient_id = ? ORDER BY n.session_date DESC, n.id DESC LIMIT ?",
                    (current_practice_id(), patient_id, limit))


def for_appointment(db: Database, appointment_id: int):
    return db.one(_latest_sql() + " WHERE n.practice_id = ? AND n.appointment_id = ? ORDER BY n.id DESC LIMIT 1",
                  (current_practice_id(), appointment_id))


def drafts_for(db: Database, author_id: int | None = None):
    sql = _latest_sql() + " JOIN patients p ON p.id = n.patient_id WHERE n.practice_id = ? AND n.status = 'draft'"
    params: list = [current_practice_id()]
    if author_id:
        sql += " AND n.author_id = ?"
        params.append(author_id)
    sql = sql.replace("SELECT n.*,", "SELECT n.*, p.first_name, p.last_name,")
    return db.query(sql + " ORDER BY n.session_date DESC, n.id DESC", params)


def versions(db: Database, note_id: int):
    return db.query("SELECT v.*, u.name AS editor_name FROM note_versions v LEFT JOIN users u ON u.id = v.edited_by"
                    " WHERE v.note_id = ? AND v.practice_id = ? ORDER BY v.version", (note_id, current_practice_id()))


def create(db: Database, patient_id: int, sections: dict, *, appointment_id: int | None = None, kind: str = "session",
           source: str = "typed", recording_id: int | None = None, session_date: str | None = None,
           author_id: int | None = None) -> int:
    now = utcnow()
    with db.tx():
        nid = db.execute(
            "INSERT INTO notes (practice_id, patient_id, appointment_id, author_id, kind, status, source, recording_id,"
            " session_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (current_practice_id(), patient_id, appointment_id, author_id or current_user_id(), kind, "draft", source,
             recording_id, session_date or today().isoformat(), now, now),
        )
        _add_version(db, nid, 1, sections, None)
        audit(db, "create", "note", nid, patient_id, {"source": source})
    return nid


def _add_version(db: Database, note_id: int, version: int, sections: dict, reason: str | None) -> None:
    db.execute(
        "INSERT INTO note_versions (practice_id, note_id, version, befund, behandlung, beurteilung, plan, reason, edited_by,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), note_id, version, *(str(sections.get(s, "") or "").strip() for s in SECTIONS), reason,
         current_user_id(), utcnow()),
    )


def save(db: Database, note_id: int, sections: dict, reason: str | None = None) -> int:
    """Neue Version speichern. Bei freigegebenen Notizen ist ein Grund (Nachtrag) Pflicht."""
    note = get(db, note_id)
    if note is None:
        raise KeyError(note_id)
    if note["status"] == "approved" and not (reason or "").strip():
        raise NoteLocked()
    if all(str(sections.get(s, "") or "").strip() == (note[s] or "") for s in SECTIONS):
        return note["version"]
    version = note["version"] + 1
    with db.tx():
        _add_version(db, note_id, version, sections, (reason or "").strip() or None)
        db.execute("UPDATE notes SET updated_at = ? WHERE id = ? AND practice_id = ?", (utcnow(), note_id, current_practice_id()))
        audit(db, "addendum" if note["status"] == "approved" else "update", "note", note_id, note["patient_id"],
              {"version": version})
    return version


def approve(db: Database, note_id: int) -> None:
    note = get(db, note_id)
    if note is None or note["status"] == "approved":
        return
    db.execute("UPDATE notes SET status = 'approved', approved_at = ?, approved_by = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), current_user_id(), utcnow(), note_id, current_practice_id()))
    audit(db, "approve", "note", note_id, note["patient_id"])


def discard_draft(db: Database, note_id: int) -> bool:
    note = get(db, note_id)
    if note is None or note["status"] != "draft":
        return False
    with db.tx():
        db.execute("DELETE FROM note_versions WHERE note_id = ? AND practice_id = ?", (note_id, current_practice_id()))
        db.execute("DELETE FROM notes WHERE id = ? AND practice_id = ? AND status = 'draft'", (note_id, current_practice_id()))
        audit(db, "delete", "note", note_id, note["patient_id"])
    return True


def draft_count(db: Database, author_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) FROM notes WHERE practice_id = ? AND status = 'draft'"
    params: list = [current_practice_id()]
    if author_id:
        sql += " AND author_id = ?"
        params.append(author_id)
    return db.value(sql, params, 0)


def appointment_ids_with_note(db: Database, since: str | None = None) -> set[int]:
    sql = "SELECT DISTINCT appointment_id FROM notes WHERE practice_id = ? AND appointment_id IS NOT NULL"
    params: list = [current_practice_id()]
    if since:
        sql += " AND session_date >= ?"
        params.append(since[:10])
    return {r["appointment_id"] for r in db.query(sql, params)}


def assessments(db: Database, patient_id: int, since: str | None = None, limit: int = 5):
    """Beurteilung und Plan der letzten freigegebenen Notizen (neueste zuerst) – für Berichte."""
    sql = (_latest_sql().replace("u.name AS author_name", "u.name AS author_name, u.lang AS author_lang")
           + " WHERE n.practice_id = ? AND n.patient_id = ? AND n.status = 'approved'")
    params: list = [current_practice_id(), patient_id]
    if since:
        sql += " AND n.session_date >= ?"
        params.append(since[:10])
    return db.query(sql + " ORDER BY n.session_date DESC, n.id DESC LIMIT ?", (*params, limit))


# --------------------------------------------------------------------------- Vorlagen
TEMPLATE_LANGS = ("de", "fr")


def template(db: Database, template_id: int, user_id: int | None = None):
    """Vorlage der Praxis oder eigene Vorlage (user_id) – fremde persönliche Vorlagen gibt es nicht."""
    return db.one("SELECT * FROM note_templates WHERE id = ? AND practice_id = ? AND (owner_id IS NULL OR owner_id = ?)",
                  (template_id, current_practice_id(), user_id))


def templates(db: Database, lang: str | None = None, user_id: int | None = None, *, practice_only: bool = False,
              own_only: bool = False):
    """Vorlagen zur Auswahl: Praxis-Vorlagen und eigene (user_id), optional nur einer Sprache."""
    sql = "SELECT t.*, u.name AS owner_name FROM note_templates t LEFT JOIN users u ON u.id = t.owner_id WHERE t.practice_id = ?"
    params: list = [current_practice_id()]
    if practice_only:
        sql += " AND t.owner_id IS NULL"
    elif own_only:
        sql += " AND t.owner_id = ?"
        params.append(user_id)
    else:
        sql += " AND (t.owner_id IS NULL OR t.owner_id = ?)"
        params.append(user_id)
    if lang:
        sql += " AND t.lang = ?"
        params.append(lang)
    return db.query(sql + " ORDER BY t.owner_id IS NULL, t.name COLLATE NOCASE", params)


def save_template(db: Database, values: dict, *, owner_id: int | None, template_id: int | None = None) -> int | None:
    """Neue Vorlage oder ändern (nur eigene bzw. – mit owner_id None – die der Praxis). Gibt die Nummer zurück (None = nicht erlaubt)."""
    fields = {"name": str(values.get("name") or "").strip()[:80],
              "lang": values.get("lang") if values.get("lang") in TEMPLATE_LANGS else "de",
              **{s: str(values.get(s) or "").strip()[:4000] for s in SECTIONS}}
    if not fields["name"]:
        return None
    now = utcnow()
    if template_id is None:
        tid = db.execute("INSERT INTO note_templates (practice_id, owner_id, name, lang, befund, behandlung, beurteilung, plan,"
                         " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (current_practice_id(), owner_id, fields["name"], fields["lang"], *(fields[s] for s in SECTIONS), now, now))
        audit(db, "create", "note_template", tid, None, {"practice": owner_id is None})
        return tid
    row = db.one("SELECT id FROM note_templates WHERE id = ? AND practice_id = ? AND owner_id IS ?", (template_id, current_practice_id(), owner_id))
    if row is None:
        return None
    db.execute("UPDATE note_templates SET name = ?, lang = ?, befund = ?, behandlung = ?, beurteilung = ?, plan = ?, updated_at = ?"
               " WHERE id = ? AND practice_id = ? AND owner_id IS ?",
               (fields["name"], fields["lang"], *(fields[s] for s in SECTIONS), now, template_id, current_practice_id(), owner_id))
    audit(db, "update", "note_template", template_id, None)
    return template_id


def delete_template(db: Database, template_id: int, *, owner_id: int | None) -> bool:
    row = db.one("SELECT id FROM note_templates WHERE id = ? AND practice_id = ? AND owner_id IS ?", (template_id, current_practice_id(), owner_id))
    if row is None:
        return False
    db.execute("DELETE FROM note_templates WHERE id = ? AND practice_id = ? AND owner_id IS ?", (template_id, current_practice_id(), owner_id))
    audit(db, "delete", "note_template", template_id, None)
    return True


# --------------------------------------------------------------------------- Für die Leitung: offene Arbeit ohne Inhalte
def drafts_overview(db: Database):
    """Entwürfe (ohne Inhalt): Datum, Patient/in, Autor/in – für Leitung › Übersicht."""
    return db.query("SELECT n.id, n.session_date, n.created_at, p.first_name, p.last_name, u.name AS author_name, n.author_id"
                    " FROM notes n JOIN patients p ON p.id = n.patient_id JOIN users u ON u.id = n.author_id"
                    " WHERE n.practice_id = ? AND n.status = 'draft' ORDER BY u.name, n.session_date", (current_practice_id(),))
