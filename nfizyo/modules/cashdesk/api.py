"""Öffentliche Schnittstelle der Kasse – für andere Module (z. B. Buchhaltungs-Export)."""
from __future__ import annotations

from datetime import date, timedelta

from ...core import money
from ...core.db import Database
from ...core.tenant import current_practice_id
from ..billing import api as billing_api
from . import repo


def _split(db: Database, sale_id: int) -> dict:
    """Ertrag ohne MWST je Art und MWST eines Verkaufs; rounding = Rundung auf 5 Rappen (bar, im Total enthalten)."""
    split = {"services": 0.0, "products": 0.0, "vat": 0.0}
    for ln in db.query("SELECT kind, amount_chf, vat_rate FROM cash_sale_lines WHERE practice_id = ? AND sale_id = ?",
                       (current_practice_id(), sale_id)):
        vat = billing_api.vat_in(ln["amount_chf"], ln["vat_rate"] or 0)
        split["vat"] += vat
        split["products" if ln["kind"] == "product" else "services"] += ln["amount_chf"] - vat
    out = {k: money.rp(v) for k, v in split.items()}
    out["rounding"] = money.rp(db.value("SELECT rounding_chf FROM cash_sales WHERE practice_id = ? AND id = ?",
                                        (current_practice_id(), sale_id), 0.0) or 0.0)
    return out


def accounting_sales(db: Database, day_from: str, day_to: str) -> list[dict]:
    """Bezahlte Verkäufe (bar, Twint, Karte) im Zeitraum, mit Ertrag ohne MWST je Art und Rundung auf 5 Rappen (bar; im Total
    enthalten). Stornierte und «auf Rechnung» fehlen (die Rechnung wird in der Abrechnung gebucht). Korrekturen nach dem
    Abschluss: accounting_corrections() – «corrections» nennt sie je Verkauf (Art, gebucht am, neue Zahlungsart)."""
    rows = [dict(s) | _split(db, s["id"]) for s in db.query(
        "SELECT id, number, day, method, total_chf, vat_chf FROM cash_sales WHERE practice_id = ? AND status = 'paid'"
        " AND day BETWEEN ? AND ? ORDER BY day, id", (current_practice_id(), day_from, day_to))]
    from ...core.timeutil import from_iso

    for r in rows:
        r["corrections"] = [{"kind": c["kind"], "method": c["method"], "to_method": c["to_method"], "amount_chf": c["amount_chf"],
                             "booked_on": c["refund_day"] or from_iso(c["created_at"]).date().isoformat(), "reason": c["reason"]}
                            for c in db.query("SELECT * FROM cash_corrections WHERE practice_id = ? AND sale_id = ? ORDER BY id",
                                              (current_practice_id(), r["id"]))]
    return rows


def accounting_corrections(db: Database, day_from: str, day_to: str) -> list[dict]:
    """Korrekturen nach dem Tagesabschluss, gebucht am Tag der Korrektur (bei Geld zurück: an diesem Tag).
    kind storno: Ertrag des Verkaufs zurück (mit Aufteilung wie der Verkauf; «auf Rechnung» fehlt – das ist eine Rechnung).
    kind method: Umbuchung von «method» nach «to_method». Jede Zeile: id, booked_on, kind, number, method, to_method, amount_chf,
    day (korrigierter Tag), services, products, vat."""
    from ...core.timeutil import from_iso

    lo = (date.fromisoformat(day_from) - timedelta(days=1)).isoformat()
    out = []
    for c in db.query("SELECT c.*, s.number FROM cash_corrections c JOIN cash_sales s ON s.id = c.sale_id WHERE c.practice_id = ?"
                      " AND (c.refund_day BETWEEN ? AND ? OR c.created_at >= ?) ORDER BY c.id",
                      (current_practice_id(), day_from, day_to, lo)):
        booked = c["refund_day"] or from_iso(c["created_at"]).date().isoformat()
        if not (day_from <= booked <= day_to) or (c["kind"] == "storno" and c["method"] == "invoice"):
            continue
        row = {k: c[k] for k in ("id", "kind", "number", "method", "to_method", "amount_chf", "day", "reason")} | {"booked_on": booked}
        out.append(row | (_split(db, c["sale_id"]) if c["kind"] == "storno" else {"services": 0.0, "products": 0.0, "vat": 0.0}))
    return out


def closings_between(db: Database, day_from: str, day_to: str) -> list[dict]:
    return [dict(r) for r in db.query("SELECT day, cash_expected, cash_counted, difference, twint_total, card_total, sales FROM cash_closings"
                                      " WHERE practice_id = ? AND day BETWEEN ? AND ? ORDER BY day", (current_practice_id(), day_from, day_to))]


def checkin_visit(db: Database, patient_id: int, day: str) -> dict | None:
    """Abo-Besuch durch Check-in (ohne Mitarbeitende). Ergebnis: {"label", "left", "sessions_total", "valid_until", …} oder None."""
    sub = repo.checkin_visit(db, patient_id, day)
    return None if sub is None else {k: sub[k] for k in ("id", "label", "left", "sessions_total", "valid_until", "state")}


def active_subscription(db: Database, patient_id: int, day: str) -> dict | None:
    """Gültiges Abo (für die Anzeige in der App) oder None."""
    subs = [s for s in repo.subscriptions_for(db, patient_id, day) if s["state"] == "active"]
    if not subs:
        return None
    s = sorted(subs, key=lambda x: x["valid_until"])[0]
    return {k: s[k] for k in ("id", "label", "left", "sessions_total", "valid_until", "state", "visited_today")}
