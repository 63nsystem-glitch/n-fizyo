"""Firmenkunden: Stammdaten, Rechnungsentwurf → abschliessen (Nummer F2026-0001, unveränderbar) → PDF mit QR-Zahlteil →
Zahlung (von Hand oder über die Bankdatei der Abrechnung) → Mahnung (3 Stufen) → Storno.

Preise sind Endpreise (inkl. MWST) wie bei Selbstzahlern; MWST nur, wenn die Praxis eine MWST-Nummer hat.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ...core import money
from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import today
from ..billing import api as billing_api

PAID_TOLERANCE = 0.005
KIND_DIGIT = 9           # QR-Referenz: erste Ziffer 9 = Firmenrechnung (Abrechnung: Rechnungs-ID ohne Kennziffer)
SCOR_PREFIX = "NFF"      # Creditor Reference: RF..NFF<id>
METHODS = ("bank", "cash", "twint", "card")
LINE_KINDS = ("service", "product")
FIRM_FIELDS = ("name", "department", "contact", "street", "house_no", "zip", "city", "email", "phone", "uid", "their_reference",
               "payment_days", "lang", "note")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _float(v, default: float | None = None) -> float | None:
    try:
        return float(str(v).replace(",", ".").replace("’", "").replace("'", "").strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- Firmen
def clean_firm(form) -> tuple[dict, list[str]]:
    """Formular → Werte und Fehler-Schlüssel (firms.err.*)."""
    v = {k: (str(form.get(k) or "").strip() or None) for k in FIRM_FIELDS}
    errs = []
    if not v["name"]:
        errs.append("name")
    if v["email"] and not _EMAIL.match(v["email"]):
        errs.append("email")
    if v["uid"]:
        uid = billing_api.clean_uid(v["uid"])
        if uid is None:
            errs.append("uid")
        else:
            v["uid"] = uid
    if v["zip"] and not re.fullmatch(r"[0-9]{4,5}", v["zip"]):
        errs.append("zip")
    days = v["payment_days"]
    if days is not None:
        if not days.isdigit() or not 0 <= int(days) <= 120:
            errs.append("payment_days")
        else:
            v["payment_days"] = int(days)
    v["lang"] = v["lang"] if v["lang"] in ("de", "fr") else "de"
    for k in ("name", "department", "contact", "street", "city", "their_reference"):
        if v[k]:
            v[k] = v[k][:120]
    if v["note"]:
        v["note"] = v["note"][:1000]
    return v, errs


def save_firm(db: Database, values: dict, firm_id: int | None = None) -> int | None:
    if firm_id:
        if get_firm(db, firm_id) is None:
            return None
        db.execute(f"UPDATE firms SET {', '.join(f'{k} = ?' for k in FIRM_FIELDS)}, updated_at = ? WHERE id = ? AND practice_id = ?",
                   (*[values.get(k) for k in FIRM_FIELDS], utcnow(), firm_id, current_practice_id()))
        audit(db, "update", "firm", firm_id, None, {"name": values.get("name")})
        return firm_id
    new = db.execute(f"INSERT INTO firms (practice_id, {', '.join(FIRM_FIELDS)}, created_at) VALUES (?, {', '.join('?' * len(FIRM_FIELDS))}, ?)",
                     (current_practice_id(), *[values.get(k) for k in FIRM_FIELDS], utcnow()))
    audit(db, "create", "firm", new, None, {"name": values.get("name")})
    return new


def set_active(db: Database, firm_id: int, active: bool) -> None:
    db.execute("UPDATE firms SET active = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
               (1 if active else 0, utcnow(), firm_id, current_practice_id()))
    audit(db, "activate" if active else "archive", "firm", firm_id)


def get_firm(db: Database, firm_id: int | None):
    if not firm_id:
        return None
    return db.one("SELECT * FROM firms WHERE id = ? AND practice_id = ?", (firm_id, current_practice_id()))


def firms(db: Database, *, active_only: bool = True, q: str | None = None):
    sql = ("SELECT f.*, (SELECT COUNT(*) FROM firm_invoices i WHERE i.firm_id = f.id AND i.status = 'draft') AS drafts,"
           " (SELECT COUNT(*) FROM firm_invoices i WHERE i.firm_id = f.id AND i.status != 'draft') AS finals"
           " FROM firms f WHERE f.practice_id = ?")
    params: list = [current_practice_id()]
    if active_only:
        sql += " AND f.active = 1"
    if q:
        sql += " AND (f.name LIKE ? OR f.city LIKE ? OR f.contact LIKE ?)"
        params += [f"%{q}%"] * 3
    return db.query(sql + " ORDER BY f.active DESC, f.name COLLATE NOCASE", params)


def debtor(firm) -> dict:
    """Adresse für Rechnung und QR-Zahlteil: Firma, z. Hd. Abteilung/Person."""
    return {"name": firm["name"], "street": firm["street"], "house_no": firm["house_no"], "zip": firm["zip"], "city": firm["city"]}


def address_lines(firm) -> list[str]:
    lines = [firm["name"]]
    for extra in (firm["department"], firm["contact"]):
        if extra:
            lines.append(extra)
    base = billing_api.address_lines(firm["name"], firm["street"], firm["house_no"], firm["zip"], firm["city"])
    return lines + base[1:]


# --------------------------------------------------------------------------- Rechnungen
def _select() -> str:
    return ("SELECT i.*, f.name AS firm_name, f.email AS firm_email, f.lang AS firm_lang, u.name AS creator_name FROM firm_invoices i"
            " JOIN firms f ON f.id = i.firm_id LEFT JOIN users u ON u.id = i.created_by")


def get(db: Database, iid: int | None):
    if not iid:
        return None
    return db.one(_select() + " WHERE i.id = ? AND i.practice_id = ?", (iid, current_practice_id()))


def listing(db: Database, *, status: str | None = None, firm_id: int | None = None, limit: int = 500):
    sql = _select() + " WHERE i.practice_id = ?"
    params: list = [current_practice_id()]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if firm_id:
        sql += " AND i.firm_id = ?"
        params.append(firm_id)
    return db.query(sql + " ORDER BY CASE i.status WHEN 'draft' THEN 0 ELSE 1 END, COALESCE(i.finalized_at, i.created_at) DESC, i.id DESC"
                    " LIMIT ?", (*params, limit))


def lines(db: Database, iid: int):
    return db.query("SELECT * FROM firm_invoice_lines WHERE practice_id = ? AND invoice_id = ? ORDER BY id", (current_practice_id(), iid))


def create(db: Database, firm, *, subject: str | None = None) -> int:
    iid = db.execute("INSERT INTO firm_invoices (practice_id, firm_id, subject, their_reference, lang, created_by, created_at)"
                     " VALUES (?,?,?,?,?,?,?)", (current_practice_id(), firm["id"], (subject or "").strip()[:160] or None,
                                                 firm["their_reference"], firm["lang"] or "de", current_user_id(), utcnow()))
    audit(db, "create", "firm_invoice", iid, None, {"firm": firm["id"]})
    return iid


def update_head(db: Database, inv, *, subject: str | None, their_reference: str | None, period_from: str | None,
                period_to: str | None, lang: str | None) -> bool:
    if inv["status"] != "draft":
        return False
    pf = period_from if _iso(period_from) else None
    pt = period_to if _iso(period_to) else None
    if pf and pt and pt < pf:
        pf, pt = pt, pf
    db.execute("UPDATE firm_invoices SET subject = ?, their_reference = ?, period_from = ?, period_to = ?, lang = ? WHERE id = ?"
               " AND practice_id = ? AND status = 'draft'",
               ((subject or "").strip()[:160] or None, (their_reference or "").strip()[:80] or None, pf, pt,
                lang if lang in ("de", "fr") else inv["lang"], inv["id"], current_practice_id()))
    return True


def _iso(v: str | None) -> bool:
    try:
        date.fromisoformat(v or "")
        return True
    except ValueError:
        return False


def vat_allowed(db: Database) -> bool:
    return bool(billing_api.vat_number(db))


def add_item_line(db: Database, inv, item, quantity: float, lang: str, service_date: str | None = None) -> int | None:
    """Position aus der Preisliste (Leistung, Produkt oder Abo) – Preis und MWST-Satz von dort."""
    if inv["status"] != "draft" or item is None or not (0 < quantity <= 10000):
        return None
    kind = "product" if item["kind"] == "product" else "service"
    return _insert(db, inv, label=billing_api.item_label(item, lang), code=item["code"], kind=kind, quantity=quantity,
                   unit_price=float(item["price_chf"]), vat_rate=billing_api.vat_rate(db, item), price_item_id=item["id"],
                   service_date=service_date)


def add_free_line(db: Database, inv, *, label: str, quantity: float, unit_price: float, vat_rate: float, kind: str,
                  service_date: str | None = None) -> int | None:
    """Freie Position (z. B. «Rückenschule im Betrieb, 2 Stunden»). MWST-Satz nur aus der Liste und nur mit MWST-Nummer."""
    label = (label or "").strip()[:200]
    if inv["status"] != "draft" or not label or not (0 < quantity <= 10000) or not (0 <= unit_price <= 100000):
        return None
    if vat_rate not in billing_api.VAT_RATES:
        return None
    return _insert(db, inv, label=label, code=None, kind=kind if kind in LINE_KINDS else "service", quantity=quantity,
                   unit_price=unit_price, vat_rate=vat_rate if vat_allowed(db) else 0.0, price_item_id=None, service_date=service_date)


def _insert(db: Database, inv, *, label, code, kind, quantity, unit_price, vat_rate, price_item_id, service_date) -> int:
    sd = service_date if _iso(service_date) else None
    lid = db.execute("INSERT INTO firm_invoice_lines (practice_id, invoice_id, service_date, price_item_id, code, label, kind, quantity,"
                     " unit_price, vat_rate, amount_chf) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (current_practice_id(), inv["id"], sd, price_item_id, code, label, kind, money.rp(quantity), money.rp(unit_price),
                      float(vat_rate or 0), money.times(money.rp(quantity), money.rp(unit_price))))
    recalc(db, inv["id"])
    return lid


def update_line(db: Database, inv, line_id: int, *, quantity: float, unit_price: float | None = None) -> bool:
    if inv["status"] != "draft" or not (0 < quantity <= 10000):
        return False
    ln = db.one("SELECT * FROM firm_invoice_lines WHERE id = ? AND invoice_id = ? AND practice_id = ?", (line_id, inv["id"], current_practice_id()))
    if ln is None:
        return False
    price = ln["unit_price"] if unit_price is None or not (0 <= unit_price <= 100000) else money.rp(unit_price)
    db.execute("UPDATE firm_invoice_lines SET quantity = ?, unit_price = ?, amount_chf = ? WHERE id = ?",
               (money.rp(quantity), price, money.times(money.rp(quantity), price), line_id))
    recalc(db, inv["id"])
    return True


def remove_line(db: Database, inv, line_id: int) -> None:
    if inv["status"] != "draft":
        return
    db.execute("DELETE FROM firm_invoice_lines WHERE id = ? AND invoice_id = ? AND practice_id = ?", (line_id, inv["id"], current_practice_id()))
    recalc(db, inv["id"])


def recalc(db: Database, iid: int) -> None:
    lns = lines(db, iid)
    total = money.total(ln["amount_chf"] for ln in lns)
    vat = money.total(v["vat"] for v in billing_api.vat_summary(lns))
    db.execute("UPDATE firm_invoices SET total_chf = ?, vat_chf = ? WHERE id = ? AND practice_id = ? AND status = 'draft'",
               (total, vat, iid, current_practice_id()))


def problems(db: Database, inv, lns) -> list[str]:
    """Was vor dem Abschluss fehlt (firms.problem.*)."""
    out = []
    if not lns:
        out.append("no_lines")
    elif inv["total_chf"] <= 0:
        out.append("zero_total")
    firm = get_firm(db, inv["firm_id"])
    if firm is None or not (firm["zip"] and firm["city"]):
        out.append("firm_address")
    out += billing_api.payment_problems(db)
    return out


def _next_number(db: Database) -> str:
    year = today().year
    n = db.value("SELECT COUNT(*) FROM firm_invoices WHERE practice_id = ? AND number LIKE ?", (current_practice_id(), f"F{year}-%"), 0)
    return f"F{year}-{n + 1:04d}"


def due_days(db: Database, firm) -> int:
    if firm is not None and firm["payment_days"] is not None:
        return int(firm["payment_days"])
    return int(billing_api.payment_settings(db)["payment_days"])


def finalize(db: Database, inv) -> str | None:
    if inv["status"] != "draft" or problems(db, inv, lines(db, inv["id"])):
        return None
    iban = billing_api.payment_iban(db)
    firm = get_firm(db, inv["firm_id"])
    due = (today() + timedelta(days=due_days(db, firm))).isoformat()
    with db.tx():
        number = _next_number(db)
        if not db.changes("UPDATE firm_invoices SET status = 'final', number = ?, finalized_at = ?, finalized_by = ?, iban = ?,"
                          " qr_reference = ?, due_on = ?, vat_number = ? WHERE id = ? AND practice_id = ? AND status = 'draft'",
                          (number, utcnow(), current_user_id(), iban,
                           billing_api.reference(iban, KIND_DIGIT, SCOR_PREFIX, inv["id"]) if iban else None,
                           due, billing_api.vat_number(db) if inv["vat_chf"] else None, inv["id"], current_practice_id())):
            return None                       # inzwischen schon abgeschlossen (zweites Fenster, Doppelklick)
        audit(db, "finalize", "firm_invoice", inv["id"], None, {"number": number, "total": inv["total_chf"]})
    return number


def cancel(db: Database, inv, reason: str) -> bool:
    if inv["status"] != "final" or not reason:
        return False
    db.execute("UPDATE firm_invoices SET status = 'cancelled', cancelled_at = ?, cancel_reason = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), reason[:300], inv["id"], current_practice_id()))
    audit(db, "cancel", "firm_invoice", inv["id"], None, {"reason": reason[:300]})
    return True


def delete_draft(db: Database, inv) -> bool:
    if inv["status"] != "draft":
        return False
    with db.tx():
        db.execute("DELETE FROM firm_invoice_lines WHERE invoice_id = ? AND practice_id = ?", (inv["id"], current_practice_id()))
        db.execute("DELETE FROM firm_invoices WHERE id = ? AND practice_id = ? AND status = 'draft'", (inv["id"], current_practice_id()))
        audit(db, "delete", "firm_invoice", inv["id"])
    return True


# --------------------------------------------------------------------------- Zahlungen und Mahnungen
def payments_for(db: Database, iid: int):
    return db.query("SELECT * FROM firm_payments WHERE practice_id = ? AND invoice_id = ? ORDER BY booked_on, id", (current_practice_id(), iid))


def reminders_for(db: Database, iid: int):
    return db.query("SELECT r.*, u.name AS creator_name FROM firm_reminders r LEFT JOIN users u ON u.id = r.created_by"
                    " WHERE r.practice_id = ? AND r.invoice_id = ? ORDER BY r.level", (current_practice_id(), iid))


def balance(db: Database, inv) -> dict:
    paid = money.rp(db.value("SELECT COALESCE(SUM(amount_chf), 0) FROM firm_payments WHERE practice_id = ? AND invoice_id = ?",
                             (current_practice_id(), inv["id"]), 0.0))
    fees = money.rp(db.value("SELECT COALESCE(SUM(fee_chf), 0) FROM firm_reminders WHERE practice_id = ? AND invoice_id = ?",
                             (current_practice_id(), inv["id"]), 0.0))
    open_ = money.rp(inv["total_chf"] + fees - paid) if inv["status"] == "final" else 0.0
    if inv["status"] in ("draft", "cancelled"):
        state = inv["status"]
    elif open_ <= PAID_TOLERANCE:
        state = "paid"
    elif inv["due_on"] and inv["due_on"] < today().isoformat():
        state = "overdue"
    elif paid > 0:
        state = "partial"
    else:
        state = "open"
    return {"paid": paid, "fees": fees, "open": max(open_, 0.0), "state": state,
            "overpaid": money.rp(-open_) if open_ < -PAID_TOLERANCE else 0.0}


def add_payment(db: Database, inv, *, amount: float, booked_on: str, method: str = "bank", source: str = "manual",
                reference: str | None = None, debtor: str | None = None, bank_ref: str | None = None, note: str | None = None) -> int | None:
    if inv["status"] != "final" or not (0 < amount <= 1000000) or not _iso(booked_on):
        return None
    if bank_ref and db.value("SELECT 1 FROM firm_payments WHERE practice_id = ? AND bank_ref = ?", (current_practice_id(), bank_ref)):
        return None
    pid = db.execute("INSERT INTO firm_payments (practice_id, invoice_id, amount_chf, booked_on, source, method, reference, debtor, bank_ref,"
                     " note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (current_practice_id(), inv["id"], money.rp(amount), booked_on, source, method if method in METHODS else "bank",
                      reference, (debtor or "")[:140] or None, bank_ref, (note or "")[:200] or None, current_user_id(), utcnow()))
    audit(db, "create", "firm_payment", pid, None, {"invoice": inv["id"], "amount": money.rp(amount), "source": source})
    return pid


def next_reminder(db: Database, inv) -> int | None:
    """Nächste Mahnstufe, wenn die Rechnung (bzw. die letzte Mahnung) überfällig ist. Höchstens Stufe 3."""
    if inv["status"] != "final" or balance(db, inv)["open"] <= PAID_TOLERANCE:
        return None
    rem = reminders_for(db, inv["id"])
    last_due = rem[-1]["due_on"] if rem else inv["due_on"]
    if len(rem) >= 3 or not last_due or last_due >= today().isoformat():
        return None
    return len(rem) + 1


def create_reminder(db: Database, inv, level: int) -> int | None:
    if next_reminder(db, inv) != level:
        return None
    st = billing_api.payment_settings(db)
    fee = {1: 0.0, 2: float(st["reminder_fee_2"] or 0), 3: float(st["reminder_fee_3"] or 0)}[level]
    rid = db.execute("INSERT INTO firm_reminders (practice_id, invoice_id, level, fee_chf, open_chf, due_on, created_by, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (current_practice_id(), inv["id"], level, fee, balance(db, inv)["open"],
                      (today() + timedelta(days=int(st["reminder_days"]))).isoformat(), current_user_id(), utcnow()))
    audit(db, "create", "firm_reminder", rid, None, {"invoice": inv["id"], "level": level, "fee": fee})
    return rid


def by_reference(db: Database, ref: str | None):
    """Abgeschlossene Firmenrechnung zu einer Zahlungsreferenz (QR oder RF) – oder None."""
    iid = billing_api.parse_reference(ref, KIND_DIGIT, SCOR_PREFIX)
    inv = get(db, iid) if iid else None
    if inv is None or (inv["qr_reference"] or "") != re.sub(r"\s", "", ref or "").upper():
        return None
    return inv


def open_items(db: Database) -> list[dict]:
    out = []
    for inv in listing(db, status="final", limit=5000):
        b = balance(db, inv)
        if b["open"] > PAID_TOLERANCE or b["overpaid"]:
            out.append({"inv": inv, "b": b, "level": len(reminders_for(db, inv["id"])), "next": next_reminder(db, inv)})
    return sorted(out, key=lambda x: (x["inv"]["due_on"] or "", x["inv"]["id"]))


def totals(db: Database) -> dict:
    """Zahlen für die Übersicht: Entwürfe, offene Posten, überfällig."""
    items = open_items(db)
    return {"drafts": db.value("SELECT COUNT(*) FROM firm_invoices WHERE practice_id = ? AND status = 'draft'", (current_practice_id(),), 0),
            "open": len([x for x in items if x["b"]["open"] > PAID_TOLERANCE]),
            "open_chf": round(sum(x["b"]["open"] for x in items), 2),
            "overdue": len([x for x in items if x["b"]["state"] == "overdue"])}
