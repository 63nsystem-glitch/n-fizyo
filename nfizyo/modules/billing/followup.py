"""Nach dem Senden: Antworten der Versicherer (Rückweisungen, Mitteilungen, Annahmen), Korrektur, Kostengutsprachen.

Rückweisung → Ursache beheben:
- Stammdaten falsch (z. B. Versichertennummer, GLN des Versicherers): im Stammblatt korrigieren, dann «Erneut senden»
  (die XML-Rechnung wird aus den aktuellen Daten neu erzeugt, gleiche Rechnungsnummer).
- Positionen falsch: «Storno und neuer Entwurf» – die Rechnung wird storniert, ein Entwurf mit denselben Zeilen entsteht,
  wird korrigiert, neu abgeschlossen und gesendet.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from . import repo, response
from ...core.timeutil import today as zurich_today

KVG_FREE_SESSIONS = 36   # Art. 5 Abs. 4 KLV: ab der 37. Sitzung Kostengutsprache des Versicherers (Vertrauensarzt)


# --------------------------------------------------------------------------- Antworten einlesen
def import_responses(db: Database, filename: str, data: bytes, *, source: str = "upload") -> dict:
    files = response.files_from_upload(filename, data)
    result = {"files": len(files), "imported": 0, "duplicates": 0, "unmatched": 0, "errors": []}
    for name, content in files:
        try:
            resp = response.parse(content)
        except response.ResponseError as exc:
            result["errors"].append((name, str(exc)))
            continue
        rid, matched = store(db, resp, content, name, source)
        if rid is None:
            result["duplicates"] += 1
            continue
        result["imported"] += 1
        if not matched:
            result["unmatched"] += 1
    audit(db, "import", "invoice_responses", None, None, {k: v for k, v in result.items() if k != "errors"} | {"errors": len(result["errors"])})
    return result


def store(db: Database, resp: response.Response, raw: bytes, filename: str, source: str) -> tuple[int | None, bool]:
    sha = hashlib.sha256(raw).hexdigest()
    if db.value("SELECT 1 FROM invoice_responses WHERE practice_id = ? AND sha256 = ?", (current_practice_id(), sha)):
        return None, False
    inv = db.one("SELECT id, patient_id FROM invoices WHERE practice_id = ? AND number = ? AND status != 'draft'",
                 (current_practice_id(), resp.request_id))
    notices = [{"code": n.code, "text": n.text, "error_value": n.error_value, "valid_value": n.valid_value, "record_id": n.record_id}
               for n in resp.notices]
    rid = db.execute(
        "INSERT INTO invoice_responses (practice_id, invoice_id, request_id, request_type, reminder_level, tiers, kind, status_in,"
        " status_out, explanation, notices, amount_due, amount_paid, insurer_gln, insurer_name, modus, encrypted, response_at, source,"
        " filename, sha256, xml, handled_at, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), inv["id"] if inv else None, resp.request_id[:35], resp.request_type, resp.reminder_level, resp.tiers,
         resp.kind, resp.status_in, resp.status_out, (resp.explanation or "")[:2800] or None, json.dumps(notices, ensure_ascii=False),
         resp.balance.get("amount_due"), resp.balance.get("amount_paid"), resp.insurer_gln, (resp.insurer_name or "")[:120] or None,
         resp.modus, 1 if resp.encrypted else 0, resp.response_at, source, filename[:200], sha, raw.decode("utf-8", "replace"),
         # Annahmen ohne Hinweise brauchen keine Bearbeitung
         utcnow() if resp.kind == "accepted" and not notices else None, current_user_id(), utcnow()))
    audit(db, "receive", "invoice_response", rid, inv["patient_id"] if inv else None,
          {"invoice": inv["id"] if inv else None, "kind": resp.kind, "tiers": resp.tiers})
    return rid, inv is not None


def _select() -> str:
    return ("SELECT r.*, i.number, i.status AS invoice_status, i.total_chf, p.first_name, p.last_name, u.name AS handler_name"
            " FROM invoice_responses r LEFT JOIN invoices i ON i.id = r.invoice_id LEFT JOIN patients p ON p.id = i.patient_id"
            " LEFT JOIN users u ON u.id = r.handled_by")


def responses_for(db: Database, invoice_id: int) -> list[dict]:
    return [_decode(r) for r in db.query(_select() + " WHERE r.practice_id = ? AND r.invoice_id = ? ORDER BY r.id DESC",
                                         (current_practice_id(), invoice_id))]


def open_responses(db: Database) -> list[dict]:
    """Zu bearbeiten: Rückweisungen, Mitteilungen, Antworten ohne passende Rechnung."""
    return [_decode(r) for r in db.query(_select() + " WHERE r.practice_id = ? AND r.handled_at IS NULL ORDER BY r.id DESC",
                                         (current_practice_id(),))]


def recent_handled(db: Database, limit: int = 20) -> list[dict]:
    return [_decode(r) for r in db.query(_select() + " WHERE r.practice_id = ? AND r.handled_at IS NOT NULL ORDER BY r.id DESC LIMIT ?",
                                         (current_practice_id(), limit))]


def get_response(db: Database, rid: int):
    row = db.one(_select() + " WHERE r.id = ? AND r.practice_id = ?", (rid, current_practice_id()))
    return _decode(row) if row else None


def _decode(row) -> dict:
    d = dict(row)
    try:
        d["notices"] = json.loads(row["notices"] or "[]")
    except ValueError:
        d["notices"] = []
    return d


def count_open_rejections(db: Database) -> int:
    return db.value("SELECT COUNT(*) FROM invoice_responses WHERE practice_id = ? AND handled_at IS NULL AND kind IN ('rejected', 'notification')",
                    (current_practice_id(),), 0)


def mark_handled(db: Database, rid: int, note: str | None) -> bool:
    r = get_response(db, rid)
    if r is None or r["handled_at"]:
        return False
    db.execute("UPDATE invoice_responses SET handled_at = ?, handled_by = ?, handled_note = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), current_user_id(), (note or "")[:300] or None, rid, current_practice_id()))
    audit(db, "handle", "invoice_response", rid, None, {"invoice": r["invoice_id"]})
    return True


def assign_response(db: Database, rid: int, number: str) -> bool:
    """Antwort ohne Zuordnung von Hand einer Rechnung zuweisen."""
    inv = db.one("SELECT id FROM invoices WHERE practice_id = ? AND number = ? AND status != 'draft'", (current_practice_id(), number.strip()))
    if inv is None:
        return False
    db.execute("UPDATE invoice_responses SET invoice_id = ? WHERE id = ? AND practice_id = ? AND invoice_id IS NULL",
               (inv["id"], rid, current_practice_id()))
    audit(db, "assign", "invoice_response", rid, None, {"invoice": inv["id"]})
    return True


# --------------------------------------------------------------------------- Korrektur
def copy_to_draft(db: Database, inv, reason: str) -> int | None:
    """Abgeschlossene Rechnung stornieren und als Entwurf (gleiche Zeilen) neu anlegen."""
    if inv["status"] != "final" or not reason:
        return None
    lns = repo.lines(db, inv["id"])
    with db.tx():
        repo.cancel(db, inv, reason)
        new = db.execute(
            "INSERT INTO invoices (practice_id, patient_id, insurer_id, prescription_id, law, tariff, tiers, case_number, accident_date,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (current_practice_id(), inv["patient_id"], inv["insurer_id"], inv["prescription_id"], inv["law"], inv["tariff"], inv["tiers"],
             inv["case_number"], inv["accident_date"], current_user_id(), utcnow()))
        for ln in lns:
            db.execute(
                "INSERT INTO invoice_lines (practice_id, invoice_id, appointment_id, service_date, therapist_id, code, label, quantity, tp,"
                " tp_value, external_factor, unit_price, vat_rate, price_item_id, amount_chf) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (current_practice_id(), new, ln["appointment_id"], ln["service_date"], ln["therapist_id"], ln["code"], ln["label"],
                 ln["quantity"], ln["tp"], ln["tp_value"], ln["external_factor"], ln["unit_price"], ln["vat_rate"], ln["price_item_id"],
                 ln["amount_chf"]))
        repo.recalc(db, new)
        db.execute("UPDATE invoice_responses SET handled_at = COALESCE(handled_at, ?), handled_by = COALESCE(handled_by, ?),"
                   " handled_note = COALESCE(handled_note, ?) WHERE practice_id = ? AND invoice_id = ?",
                   (utcnow(), current_user_id(), f"→ Entwurf #{new}", current_practice_id(), inv["id"]))
        audit(db, "correct", "invoice", inv["id"], inv["patient_id"], {"new_draft": new, "reason": reason[:200]})
    return new


# --------------------------------------------------------------------------- Kostengutsprachen
APPROVAL_STATUSES = ("requested", "approved", "partial", "rejected")


def approvals_for(db: Database, patient_id: int):
    return db.query("SELECT c.*, ins.name AS insurer_name FROM cost_approvals c LEFT JOIN insurers ins ON ins.id = c.insurer_id"
                    " WHERE c.practice_id = ? AND c.patient_id = ? ORDER BY c.requested_on DESC, c.id DESC",
                    (current_practice_id(), patient_id))


def approval(db: Database, cid: int):
    return db.one("SELECT * FROM cost_approvals WHERE id = ? AND practice_id = ?", (cid, current_practice_id()))


def add_approval(db: Database, patient, values: dict) -> int:
    cid = db.execute(
        "INSERT INTO cost_approvals (practice_id, patient_id, insurer_id, prescription_id, requested_on, sessions_requested, reference, note,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient["id"], patient["insurer_id"], values.get("prescription_id"), values["requested_on"],
         values.get("sessions_requested"), values.get("reference"), values.get("note"), current_user_id(), utcnow()))
    audit(db, "create", "cost_approval", cid, patient["id"], {"sessions": values.get("sessions_requested")})
    return cid


def decide_approval(db: Database, cid: int, values: dict) -> bool:
    row = approval(db, cid)
    if row is None or values.get("status") not in APPROVAL_STATUSES:
        return False
    db.execute("UPDATE cost_approvals SET status = ?, sessions_approved = ?, valid_until = ?, decided_on = ?, reference = COALESCE(?, reference),"
               " note = COALESCE(?, note), updated_at = ? WHERE id = ? AND practice_id = ?",
               (values["status"], values.get("sessions_approved"), values.get("valid_until"),
                values.get("decided_on") or (zurich_today().isoformat() if values["status"] != "requested" else None),
                values.get("reference"), values.get("note"), utcnow(), cid, current_practice_id()))
    audit(db, "decide", "cost_approval", cid, row["patient_id"], {"status": values["status"], "sessions": values.get("sessions_approved")})
    return True


def kvg_sessions(db: Database, patient_id: int, *, extra_invoice_id: int | None = None, days: int = 365) -> int:
    """KVG-Sitzungen (verrechnete Termine) der letzten 12 Monate – Näherung für «fortlaufende Therapie»."""
    since = (zurich_today() - timedelta(days=days)).isoformat()
    return db.value(
        "SELECT COUNT(DISTINCT l.appointment_id) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id"
        " WHERE i.practice_id = ? AND i.patient_id = ? AND i.law = 'KVG' AND i.status != 'cancelled' AND l.appointment_id IS NOT NULL"
        " AND l.service_date >= ? AND (i.status = 'final' OR i.id = ?)",
        (current_practice_id(), patient_id, since, extra_invoice_id or 0), 0)


def has_valid_approval(db: Database, patient_id: int, on: str) -> bool:
    return bool(db.value("SELECT 1 FROM cost_approvals WHERE practice_id = ? AND patient_id = ? AND status IN ('approved', 'partial')"
                         " AND (valid_until IS NULL OR valid_until >= ?)", (current_practice_id(), patient_id, on)))


def warnings(db: Database, inv) -> list[str]:
    """Hinweise (blockieren den Abschluss nicht)."""
    out = []
    if inv["law"] == "KVG" and inv["tariff"] != "private":
        n = kvg_sessions(db, inv["patient_id"], extra_invoice_id=inv["id"])
        if n > KVG_FREE_SESSIONS and not has_valid_approval(db, inv["patient_id"], inv["period_to"] or zurich_today().isoformat()):
            out.append("kogu_36")
    if inv["status"] == "final" and db.value("SELECT 1 FROM invoice_responses WHERE practice_id = ? AND invoice_id = ? AND kind = 'rejected'"
                                             " AND handled_at IS NULL", (current_practice_id(), inv["id"])):
        out.append("rejected_open")
    return out
