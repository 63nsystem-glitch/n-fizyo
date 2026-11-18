"""Berichte an Zuweisende: Entwurf aus vorhandenen Daten → bearbeiten → abschliessen → als versendet markieren."""
from __future__ import annotations

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id

KINDS = ("progress", "renewal", "discharge")
SECTIONS = ("diagnosis", "course", "findings", "assessment", "recommendation")
SENT_VIA = ("hin", "post", "fax", "patient")


class ReportLocked(Exception):
    pass


def _select() -> str:
    return ("SELECT r.*, f.name AS referrer_name, f.practice_name AS referrer_practice, f.address AS referrer_address,"
            " f.hin_email AS referrer_hin, u.name AS author_name, p.first_name, p.last_name, p.birthdate, p.sex"
            " FROM reports r JOIN patients p ON p.id = r.patient_id LEFT JOIN referrers f ON f.id = r.referrer_id"
            " JOIN users u ON u.id = r.author_id")


def get(db: Database, rid: int):
    return db.one(_select() + " WHERE r.id = ? AND r.practice_id = ?", (rid, current_practice_id()))


def for_patient(db: Database, patient_id: int):
    return db.query(_select() + " WHERE r.practice_id = ? AND r.patient_id = ? ORDER BY r.id DESC",
                    (current_practice_id(), patient_id))


def drafts(db: Database, author_id: int | None = None):
    sql = _select() + " WHERE r.practice_id = ? AND r.status = 'draft'"
    params: list = [current_practice_id()]
    if author_id:
        sql += " AND r.author_id = ?"
        params.append(author_id)
    return db.query(sql + " ORDER BY r.updated_at DESC", params)


def create(db: Database, patient_id: int, *, kind: str, lang: str, referrer_id: int | None, prescription_id: int | None,
           subject: str, salutation: str, sections: dict) -> int:
    now = utcnow()
    rid = db.execute(
        "INSERT INTO reports (practice_id, patient_id, referrer_id, prescription_id, kind, lang, subject, salutation, diagnosis,"
        " course, findings, assessment, recommendation, author_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, referrer_id, prescription_id, kind, lang, subject, salutation,
         *(sections.get(s, "") for s in SECTIONS), current_user_id(), now, now),
    )
    audit(db, "create", "report", rid, patient_id, {"kind": kind})
    return rid


def save(db: Database, rid: int, fields: dict) -> None:
    r = get(db, rid)
    if r is None:
        raise KeyError(rid)
    if r["status"] != "draft":
        raise ReportLocked()
    cols = [c for c in ("subject", "salutation", *SECTIONS) if c in fields]
    if not cols:
        return
    db.execute(f"UPDATE reports SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ? WHERE id = ? AND practice_id = ?",
               (*(fields[c] for c in cols), utcnow(), rid, current_practice_id()))
    audit(db, "update", "report", rid, r["patient_id"])


def finalize(db: Database, rid: int) -> None:
    r = get(db, rid)
    if r is None or r["status"] != "draft":
        return
    if db.changes("UPDATE reports SET status = 'final', finalized_at = ?, finalized_by = ?, updated_at = ? WHERE id = ? AND practice_id = ?"
                  " AND status = 'draft'", (utcnow(), current_user_id(), utcnow(), rid, current_practice_id())):
        audit(db, "finalize", "report", rid, r["patient_id"])


def mark_sent(db: Database, rid: int, via: str) -> None:
    r = get(db, rid)
    if r is None or r["status"] == "draft" or via not in SENT_VIA:
        return
    db.execute("UPDATE reports SET status = 'sent', sent_at = ?, sent_via = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), via, rid, current_practice_id()))
    audit(db, "send", "report", rid, r["patient_id"], {"via": via})


def delete_draft(db: Database, rid: int) -> bool:
    r = get(db, rid)
    if r is None or r["status"] != "draft":
        return False
    db.execute("DELETE FROM reports WHERE id = ? AND practice_id = ? AND status = 'draft'", (rid, current_practice_id()))
    audit(db, "delete", "report", rid, r["patient_id"])
    return True


def last_since(db: Database, patient_id: int, since: str | None):
    return db.one("SELECT * FROM reports WHERE practice_id = ? AND patient_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
                  (current_practice_id(), patient_id, since or "0000"))
