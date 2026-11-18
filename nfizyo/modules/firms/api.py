"""Öffentliche Schnittstelle der Firmenkunden – für die Abrechnung (Bankdatei), den Export (Buchhaltung, bexio) und die
Übersicht der Leitung."""
from __future__ import annotations

from ...core import money
from ...core.db import Database
from ...core.tenant import current_practice_id
from ..billing import api as billing_api
from . import repo


def claim_payment(db: Database, *, reference: str | None, amount: float, booked_on: str, debtor: str | None,
                  bank_ref: str | None) -> bool | None:
    """Bankbuchung aus der camt-Datei der Abrechnung: gehört die Referenz zu einer Firmenrechnung, wird die Zahlung hier
    gebucht. True = gebucht, False = schon vorhanden (Doppel), None = keine Firmenrechnung."""
    inv = repo.by_reference(db, reference)
    if inv is None:
        return None
    return repo.add_payment(db, inv, amount=amount, booked_on=booked_on, source="camt", reference=reference, debtor=debtor,
                            bank_ref=bank_ref) is not None


def _split(db: Database, invoice_id: int) -> dict:
    out = {"services": 0.0, "products": 0.0, "vat": 0.0}
    for ln in repo.lines(db, invoice_id):
        vat = billing_api.vat_in(ln["amount_chf"], ln["vat_rate"] or 0)
        out["vat"] += vat
        out["products" if ln["kind"] == "product" else "services"] += ln["amount_chf"] - vat
    return {k: money.rp(v) for k, v in out.items()}


def accounting(db: Database, start_utc: str, end_utc: str, day_from: str, day_to: str) -> dict:
    """Für das Buchungsjournal: im Zeitraum abgeschlossene / stornierte Firmenrechnungen, Zahlungen, Mahngebühren."""
    pid = current_practice_id()
    invoices = [dict(r) | _split(db, r["id"]) for r in db.query(
        "SELECT i.id, i.number, i.finalized_at, i.total_chf, i.vat_chf, i.status, f.name AS firm_name FROM firm_invoices i"
        " JOIN firms f ON f.id = i.firm_id WHERE i.practice_id = ? AND i.finalized_at >= ? AND i.finalized_at < ? ORDER BY i.finalized_at, i.id",
        (pid, start_utc, end_utc))]
    cancellations = [dict(r) | _split(db, r["id"]) for r in db.query(
        "SELECT i.id, i.number, i.cancelled_at, i.total_chf, i.cancel_reason, f.name AS firm_name FROM firm_invoices i"
        " JOIN firms f ON f.id = i.firm_id WHERE i.practice_id = ? AND i.status = 'cancelled' AND i.cancelled_at >= ? AND i.cancelled_at < ?"
        " ORDER BY i.cancelled_at", (pid, start_utc, end_utc))]
    payments = [dict(r) for r in db.query(
        "SELECT p.id, p.booked_on, p.amount_chf, p.method, p.source, p.debtor, p.reference, i.number, f.name AS firm_name"
        " FROM firm_payments p JOIN firm_invoices i ON i.id = p.invoice_id JOIN firms f ON f.id = i.firm_id"
        " WHERE p.practice_id = ? AND p.booked_on BETWEEN ? AND ? ORDER BY p.booked_on, p.id", (pid, day_from, day_to))]
    fees = [dict(r) for r in db.query(
        "SELECT r.id, r.level, r.fee_chf, r.created_at, i.number FROM firm_reminders r JOIN firm_invoices i ON i.id = r.invoice_id"
        " WHERE r.practice_id = ? AND r.fee_chf > 0 AND r.created_at >= ? AND r.created_at < ? ORDER BY r.created_at",
        (pid, start_utc, end_utc))]
    return {"invoices": invoices, "cancellations": cancellations, "payments": payments, "fees": fees}


def open_items(db: Database) -> list[dict]:
    """Offene Posten (Firmen) im gleichen Aufbau wie die der Abrechnung."""
    return [{"number": x["inv"]["number"], "finalized_at": x["inv"]["finalized_at"], "due_on": x["inv"]["due_on"],
             "name": x["inv"]["firm_name"], "total": x["inv"]["total_chf"], "open": x["b"]["open"], "state": x["b"]["state"],
             "level": x["level"]} for x in repo.open_items(db)]


def totals(db: Database) -> dict:
    return repo.totals(db)
