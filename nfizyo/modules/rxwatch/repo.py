"""Daten für «Verordnungen im Blick»: aktive Verordnungen, Nutzung (über die Agenda), Anfragen an Ärztinnen und Ärzte."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import from_iso, today
from ..agenda import api as agenda_api
from . import rules

KINDS = ("renewal", "kogu", "correction", "new")
VIAS = ("phone", "fax", "mail", "hin", "report", "other")

_SELECT = (
    "SELECT pr.*, p.first_name, p.last_name, p.number, p.law, p.therapist_id AS main_therapist_id, p.archived,"
    " r.name AS referrer_name, r.phone AS referrer_phone, u.name AS main_therapist_name"
    " FROM prescriptions pr JOIN patients p ON p.id = pr.patient_id"
    " LEFT JOIN referrers r ON r.id = pr.referrer_id LEFT JOIN users u ON u.id = p.therapist_id"
    " WHERE pr.practice_id = ? AND pr.status = 'active' AND p.archived = 0"
    " AND pr.id = (SELECT MAX(x.id) FROM prescriptions x WHERE x.practice_id = pr.practice_id AND x.patient_id = pr.patient_id"
    " AND x.status = 'active')"
)


def active(db: Database, *, only: tuple[str, list] | None = None, patient_id: int | None = None):
    """Aktuelle (neueste aktive) Verordnung je Patient/in. only=(sql, params): nur diese Patient/innen."""
    sql, params = _SELECT, [current_practice_id()]
    if only:
        sql += f" AND pr.patient_id IN ({only[0]})"
        params += list(only[1])
    if patient_id:
        sql += " AND pr.patient_id = ?"
        params.append(patient_id)
    return db.query(sql + " ORDER BY p.last_name, p.first_name", params)


def get_prescription(db: Database, prescription_id: int):
    return db.one("SELECT * FROM prescriptions WHERE id = ? AND practice_id = ?", (prescription_id, current_practice_id()))


# --------------------------------------------------------------------------- Anfragen
def open_requests(db: Database, prescription_id: int):
    return db.query("SELECT q.*, u.name AS creator_name FROM rx_requests q LEFT JOIN users u ON u.id = q.created_by"
                    " WHERE q.practice_id = ? AND q.prescription_id = ? AND q.status = 'open' ORDER BY q.id",
                    (current_practice_id(), prescription_id))


def get_request(db: Database, request_id: int):
    return db.one("SELECT * FROM rx_requests WHERE id = ? AND practice_id = ?", (request_id, current_practice_id()))


def add_request(db: Database, patient_id: int, prescription_id: int, kind: str, via: str, note: str | None = None,
                on: str | None = None) -> int:
    rid = db.execute(
        "INSERT INTO rx_requests (practice_id, patient_id, prescription_id, kind, via, note, requested_on, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, prescription_id, kind if kind in KINDS else "renewal", via if via in VIAS else "other",
         (note or "").strip()[:300] or None, on or today().isoformat(), current_user_id(), utcnow()))
    audit(db, "create", "rx_request", rid, patient_id, {"kind": kind, "via": via})
    return rid


def close_request(db: Database, request_id: int, status: str = "done") -> None:
    row = get_request(db, request_id)
    if row is None or row["status"] != "open":
        return
    db.execute("UPDATE rx_requests SET status = ?, closed_at = ?, closed_by = ? WHERE id = ? AND practice_id = ?",
               (status if status in ("done", "withdrawn") else "done", utcnow(), current_user_id(), request_id, current_practice_id()))
    audit(db, "update", "rx_request", request_id, row["patient_id"], {"status": status})


def history(db: Database, patient_id: int, limit: int = 10):
    return db.query("SELECT q.*, u.name AS creator_name FROM rx_requests q LEFT JOIN users u ON u.id = q.created_by"
                    " WHERE q.practice_id = ? AND q.patient_id = ? ORDER BY q.id DESC LIMIT ?", (current_practice_id(), patient_id, limit))


# --------------------------------------------------------------------------- Auswertung
# Gezählt wird mit der gemeinsamen Regel der Agenda (agenda_api.rx_usage / used_sessions): schon in der bisherigen Software
# durchgeführt (sessions_before) + erledigt + angemeldet + vergangene, nicht abgesagte Termine. So zeigen Agenda («10/9») und
# diese Liste denselben Stand – auch wenn ein vergangener Termin noch nicht als erledigt markiert ist.
BULK = 150   # ab so vielen Verordnungen: alle Zahlen der Praxis in einer Abfrage statt einer langen ID-Liste


def _prefetch(db: Database, rows, day: date) -> dict:
    """Alles, was evaluate() braucht, für viele Verordnungen auf einmal (wenige Abfragen statt mehrerer pro Verordnung)."""
    pid = current_practice_id()
    rx_ids = [r["id"] for r in rows]
    patient_ids = sorted({r["patient_id"] for r in rows})
    bulk = len(rows) > BULK
    since = (day - timedelta(days=365)).isoformat()
    sql = ("SELECT id, patient_id FROM prescriptions WHERE practice_id = ? AND COALESCE(issued_on, substr(created_at, 1, 10)) >= ?")
    params: list = [pid, since]
    if not bulk:
        sql += " AND patient_id IN (" + ",".join("?" for _ in patient_ids) + ")"
        params += patient_ids
    chain_rows = db.query(sql, params)
    usage = agenda_api.rx_usage(db, None if bulk else set(rx_ids) | {r["id"] for r in chain_rows})
    chain: dict[int, int] = {}
    for r in chain_rows:
        chain[r["patient_id"]] = chain.get(r["patient_id"], 0) + usage.get(r["id"], {}).get("used", 0)
    sql = ("SELECT q.*, u.name AS creator_name FROM rx_requests q LEFT JOIN users u ON u.id = q.created_by"
           " WHERE q.practice_id = ? AND q.status = 'open'")
    params = [pid]
    if not bulk:
        sql += " AND q.prescription_id IN (" + ",".join("?" for _ in rx_ids) + ")"
        params += rx_ids
    reqs: dict[int, list] = {}
    for q in db.query(sql + " ORDER BY q.id", params):
        reqs.setdefault(q["prescription_id"], []).append(q)
    return {"usage": usage, "chain": chain, "requests": reqs,
            "future": agenda_api.future_booked(db, None if bulk else patient_ids)}


def _chain_used(db: Database, patient_id: int, day: date) -> int:
    """36er-Grenze (KVG): verbrauchte Sitzungen der letzten 12 Monate über alle Verordnungen einer Person."""
    return _prefetch(db, [{"id": 0, "patient_id": patient_id}], day)["chain"].get(patient_id, 0)


def evaluate_many(db: Database, rows, *, renewal_of=None, approval_of=None, day: date | None = None) -> list[dict]:
    """Stand vieler Verordnungen (rows aus active()) mit wenigen Abfragen. renewal_of(row) / approval_of(row): letztes Gesuch
    aus dem Berichte-Modul bzw. Kostengutsprache aus der Abrechnung (je oder None) – optional."""
    if not rows:
        return []
    day = day or today()
    pre = _prefetch(db, rows, day)
    return [_evaluate(db, r, pre, renewal_of(r) if renewal_of else None, approval_of(r) if approval_of else None, day) for r in rows]


def evaluate(db: Database, row, *, renewal: Any = None, approval: Any = None, day: date | None = None) -> dict:
    """Stand einer Verordnung (row aus active()). renewal: letztes Gesuch aus dem Berichte-Modul, approval: Kostengutsprache
    aus der Abrechnung (je oder None)."""
    return evaluate_many(db, [row], renewal_of=lambda _: renewal, approval_of=lambda _: approval, day=day)[0]


def _evaluate(db: Database, row, pre: dict, renewal: Any, approval: Any, day: date) -> dict:
    pr = dict(row)
    used = pre["usage"].get(pr["id"], {}).get("used", int(pr.get("sessions_before") or 0))
    booked = [a for a in pre["future"].get(pr["patient_id"], []) if a["prescription_id"] in (pr["id"], None)][:60]
    future = [from_iso(a["starts_at"]).date() for a in booked]
    open_rows = pre["requests"].get(pr["id"], [])
    requests = [{"kind": q["kind"], "date": date.fromisoformat(q["requested_on"]), "via": q["via"], "id": q["id"]} for q in open_rows]
    draft = None
    if renewal is not None:
        if renewal["status"] in ("final", "sent"):
            when = from_iso(renewal["sent_at"] or renewal["finalized_at"] or renewal["created_at"]).date()   # Zürcher Tag
            requests.append({"kind": "renewal", "date": when, "via": "report", "id": None})
        else:
            draft = renewal["id"]
    if approval is not None and approval["status"] == "requested":
        requests.append({"kind": "kogu", "date": date.fromisoformat(approval["requested_on"][:10]), "via": "other", "id": None})
    kvg = (pr.get("law") or "KVG").upper() == "KVG"
    result = rules.evaluate(rules.RxInput(
        prescription=pr, law=pr.get("law"), used=used, future=sorted(future),
        chain_used=pre["chain"].get(pr["patient_id"], 0) if kvg else used, requests=requests, renewal_draft=draft,
        kogu_ok=bool(approval and approval["valid"]), today=day))
    result["row"] = row
    result["first_future_id"] = booked[0]["id"] if booked else None
    result["requests"] = open_rows
    return result
