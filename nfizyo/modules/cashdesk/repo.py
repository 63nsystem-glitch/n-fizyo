"""Kasse: Verkauf → Quittung (unveränderbar, nur Storno) → Tagesabschluss. Abos mit Besuchen.

Bezahlt wird bar, mit Twint, mit Karte – oder «auf Rechnung»: Dann entsteht in der Abrechnung ein Entwurf
(Selbstzahler-Tarif) mit denselben Positionen, den die Leitung wie jede Rechnung abschliesst.

Bar wird auf 5 Rappen gerundet (core/money.py): total_chf = was bezahlt wurde (Positionen + rounding_chf). Twint und Karte genau.
"""
from __future__ import annotations

from datetime import date, timedelta

from ...core import money
from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ..billing import api as billing_api

METHODS = ("cash", "twint", "card", "invoice")
PAID_METHODS = ("cash", "twint", "card")
MAX_QTY = 99


class SaleError(Exception):
    """Schlüssel unter cashdesk.err.*"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- Verkauf
def _next_number(db: Database, year: int) -> str:
    n = db.value("SELECT COUNT(*) FROM cash_sales WHERE practice_id = ? AND number LIKE ?", (current_practice_id(), f"Q{year}-%"), 0)
    return f"Q{year}-{n + 1:04d}"


def is_closed(db: Database, day: str) -> bool:
    return bool(db.value("SELECT 1 FROM cash_closings WHERE practice_id = ? AND day = ?", (current_practice_id(), day)))


def receipt_lang(db: Database, patient) -> str:
    """Sprache von Quittung und Positionstexten: Deutsch/Französisch der Person, sonst die Praxissprache (nie die Bildschirmsprache)."""
    if patient is not None and patient["lang"] in ("de", "fr"):
        return patient["lang"]
    practice = db.one("SELECT default_lang FROM practices WHERE id = ?", (current_practice_id(),))
    return practice["default_lang"] if practice is not None and practice["default_lang"] in ("de", "fr") else "de"


def sell(db: Database, *, patient, entries: list[tuple], method: str, lang: str, day: str, given: float | None = None,
         note: str | None = None) -> tuple[int, int | None]:
    """Verkauf buchen. entries = [(price_item, Anzahl)]. Gibt (Quittung, Rechnungsentwurf oder None) zurück."""
    lang = receipt_lang(db, patient)
    entries = [(it, q) for it, q in entries if it is not None and q]
    if not entries:
        raise SaleError("empty")
    if method not in METHODS:
        raise SaleError("method")
    if any(not (0 < q <= MAX_QTY) or q != int(q) for _, q in entries):
        raise SaleError("qty")
    if any(it["kind"] == "abo" for it, _ in entries):
        if patient is None:
            raise SaleError("abo_patient")
        if any(it["kind"] == "abo" and q != 1 for it, q in entries):
            raise SaleError("abo_qty")
    if method == "invoice" and patient is None:
        raise SaleError("invoice_patient")
    if is_closed(db, day):
        raise SaleError("closed")
    rows = []
    for it, q in entries:
        rate = billing_api.vat_rate(db, it)
        rows.append({"item": it, "kind": it["kind"], "code": it["code"], "label": billing_api.item_label(it, lang), "quantity": float(q),
                     "unit_price": it["price_chf"], "vat_rate": rate, "amount_chf": money.times(q, it["price_chf"])})
    items_total = money.total(r["amount_chf"] for r in rows)
    total = money.cash(items_total) if method == "cash" else items_total          # bar: auf 5 Rappen
    rounding = money.rp(total - items_total)
    vat = money.total(v["vat"] for v in billing_api.vat_summary(rows))
    if method == "cash" and given is not None and given + 0.001 < total:
        raise SaleError("given")
    with db.tx():
        invoice_id = None
        if method == "invoice":
            invoice_id = billing_api.create_invoice_from_items(db, patient, entries, lang)
        number = _next_number(db, int(day[:4]))
        sid = db.execute(
            "INSERT INTO cash_sales (practice_id, number, day, patient_id, method, total_chf, rounding_chf, vat_chf, vat_number, given_chf,"
            " invoice_id, status, note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (current_practice_id(), number, day, patient["id"] if patient else None, method, total, rounding, vat, billing_api.vat_number(db),
             given if method == "cash" else None, invoice_id, "invoiced" if method == "invoice" else "paid", note,
             current_user_id(), utcnow()))
        for r in rows:
            db.execute("INSERT INTO cash_sale_lines (practice_id, sale_id, price_item_id, kind, code, label, quantity, unit_price, vat_rate,"
                       " amount_chf) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (current_practice_id(), sid, r["item"]["id"], r["kind"], r["code"], r["label"], r["quantity"], r["unit_price"],
                        r["vat_rate"], r["amount_chf"]))
            if r["kind"] == "abo":
                _create_subscription(db, patient["id"], sid, r["item"], r["label"], day)
        audit(db, "create", "cash_sale", sid, patient["id"] if patient else None,
              {"number": number, "total": total, "method": method, "invoice": invoice_id, **({"rounding": rounding} if rounding else {})})
    return sid, invoice_id


def totals(db: Database, entries: list[tuple], given: float | None = None, method: str = "cash") -> dict:
    """Vorschau vor dem Verkauf (ohne JavaScript): Total, MWST, Rundung (bar) und Rückgeld – gleiche Rechnung wie sell()."""
    rows = []
    for it, q in entries:
        if it is None or not q or q < 0 or q > MAX_QTY:
            continue
        rows.append({"vat_rate": billing_api.vat_rate(db, it), "amount_chf": money.times(q, it["price_chf"])})
    items_total = money.total(r["amount_chf"] for r in rows)
    total = money.cash(items_total) if method == "cash" else items_total
    change = money.rp(given - total) if given is not None and rows else None
    return {"total": total, "items_total": items_total, "rounding": money.rp(total - items_total),
            "vat": money.total(v["vat"] for v in billing_api.vat_summary(rows)), "given": given, "change": change}


def _select() -> str:
    return ("SELECT s.*, p.first_name, p.last_name, u.name AS creator_name FROM cash_sales s"
            " LEFT JOIN patients p ON p.id = s.patient_id JOIN users u ON u.id = s.created_by")


def get(db: Database, sid: int):
    return db.one(_select() + " WHERE s.id = ? AND s.practice_id = ?", (sid, current_practice_id()))


def lines(db: Database, sid: int):
    return db.query("SELECT * FROM cash_sale_lines WHERE practice_id = ? AND sale_id = ? ORDER BY id", (current_practice_id(), sid))


def day_sales(db: Database, day: str):
    return db.query(_select() + " WHERE s.practice_id = ? AND s.day = ? ORDER BY s.id DESC", (current_practice_id(), day))


def sales_between(db: Database, day_from: str, day_to: str):
    return db.query(_select() + " WHERE s.practice_id = ? AND s.day BETWEEN ? AND ? ORDER BY s.day, s.id", (current_practice_id(), day_from, day_to))


def for_patient(db: Database, patient_id: int, limit: int = 10):
    return db.query(_select() + " WHERE s.practice_id = ? AND s.patient_id = ? ORDER BY s.id DESC LIMIT ?",
                    (current_practice_id(), patient_id, limit))


def cancel(db: Database, sale, reason: str) -> bool | str:
    """Storno vor dem Tagesabschluss. Abos aus diesem Verkauf werden mit storniert. «Auf Rechnung»: der Rechnungsentwurf wird
    gelöscht; ist die Rechnung schon abgeschlossen → "invoice_final" (zuerst die Rechnung stornieren – sonst bliebe sie offen)."""
    if sale["status"] == "cancelled" or not reason or is_closed(db, sale["day"]):
        return False
    try:
        return _cancel(db, sale, reason)
    except _InvoiceFinal:
        return "invoice_final"                                 # alles zurückgerollt


def _cancel(db: Database, sale, reason: str) -> bool:
    with db.tx():
        if not db.changes("UPDATE cash_sales SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?, cancelled_by = ? WHERE id = ?"
                          " AND practice_id = ? AND status != 'cancelled'",
                          (utcnow(), reason[:200], current_user_id(), sale["id"], current_practice_id())):
            return False                                       # schon storniert (Doppelklick, zweites Fenster)
        if sale["method"] == "invoice" and billing_api.discard_draft_invoice(db, sale["invoice_id"]) == "final":
            raise _InvoiceFinal()
        db.execute("UPDATE subscriptions SET status = 'cancelled' WHERE sale_id = ? AND practice_id = ?", (sale["id"], current_practice_id()))
        audit(db, "cancel", "cash_sale", sale["id"], sale["patient_id"], {"number": sale["number"], "reason": reason[:200]})
    return True


class _InvoiceFinal(Exception):
    pass


# --------------------------------------------------------------------------- Tagesabschluss
def day_summary(db: Database, day: str) -> dict:
    """Einnahmen eines Tages je Zahlungsart: Verkäufe (ohne Storno) + Zahlungen auf Rechnungen an der Theke
    − Geld, das an diesem Tag für eine Quittung eines abgeschlossenen Tages zurückgegeben wurde (Korrektur).
    Dazu die Korrekturen, die NACH dem Abschluss dieses Tages gebucht wurden (der Abschluss selbst bleibt unverändert)."""
    out = {m: 0.0 for m in METHODS}
    n = 0
    for s in db.query("SELECT method, total_chf FROM cash_sales WHERE practice_id = ? AND day = ? AND status != 'cancelled'",
                      (current_practice_id(), day)):
        out[s["method"]] += s["total_chf"]
        n += 1
    payments = billing_api.desk_payments(db, day)
    for pm in payments:
        if pm["method"] in out:
            out[pm["method"]] += pm["cash_chf"]           # bar: mit Rundung auf 5 Rappen (was in der Kasse liegt)
    refunds = refunds_on(db, day)
    for r in refunds:
        out[r["method"]] -= r["amount_chf"]
    corr = corrections_for_day(db, day)
    after = {m: 0.0 for m in METHODS}
    for c in corr:
        after[c["method"]] -= c["amount_chf"]
        if c["kind"] == "method":
            after[c["to_method"]] += c["amount_chf"]
    by_method = {k: money.rp(v) for k, v in out.items()}
    return {"by_method": by_method, "sales": n, "payments": payments, "cash": money.rp(out["cash"]), "refunds": refunds,
            "corrections": corr, "correction_by_method": {k: money.rp(v) for k, v in after.items()},
            "net_by_method": {k: money.rp(by_method[k] + after[k]) for k in METHODS}}


def closing(db: Database, day: str):
    return db.one("SELECT c.*, u.name AS creator_name FROM cash_closings c JOIN users u ON u.id = c.created_by"
                  " WHERE c.practice_id = ? AND c.day = ?", (current_practice_id(), day))


def closings(db: Database, limit: int = 60):
    return db.query("SELECT c.*, u.name AS creator_name FROM cash_closings c JOIN users u ON u.id = c.created_by"
                    " WHERE c.practice_id = ? ORDER BY c.day DESC LIMIT ?", (current_practice_id(), limit))


def close_day(db: Database, day: str, counted: float, note: str | None) -> int | None:
    if is_closed(db, day):
        return None
    summary = day_summary(db, day)
    cid = db.execute(
        "INSERT INTO cash_closings (practice_id, day, cash_expected, cash_counted, difference, twint_total, card_total, sales, note,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), day, summary["cash"], money.rp(counted), money.rp(counted - summary["cash"]),
         summary["by_method"]["twint"], summary["by_method"]["card"], summary["sales"], (note or "")[:300] or None,
         current_user_id(), utcnow()))
    audit(db, "close", "cash_day", cid, None, {"day": day, "expected": summary["cash"], "counted": money.rp(counted)})
    return cid


def unclosed_days(db: Database, before: str, lookback_days: int = 45) -> list[str]:
    """Tage mit Kassenbewegungen ohne Tagesabschluss (vor «before»)."""
    since = (date.fromisoformat(before) - timedelta(days=lookback_days)).isoformat()
    days = {r["day"] for r in db.query("SELECT DISTINCT day FROM cash_sales WHERE practice_id = ? AND day >= ? AND day < ?"
                                       " AND method != 'invoice'", (current_practice_id(), since, before))}
    yesterday = (date.fromisoformat(before) - timedelta(days=1)).isoformat()
    days |= {pm["booked_on"] for pm in billing_api.desk_payments(db, since, yesterday)}
    days |= {r["refund_day"] for r in db.query("SELECT DISTINCT refund_day FROM cash_corrections WHERE practice_id = ? AND refund_day >= ?"
                                               " AND refund_day < ?", (current_practice_id(), since, before))}
    closed = {r["day"] for r in db.query("SELECT day FROM cash_closings WHERE practice_id = ? AND day >= ?", (current_practice_id(), since))}
    return sorted(days - closed)


# --------------------------------------------------------------------------- Korrektur nach dem Abschluss (Leitung)
CORRECTION_KINDS = ("storno", "method")


def _corr_select() -> str:
    return ("SELECT c.*, s.number, s.total_chf AS sale_total, u.name AS creator_name FROM cash_corrections c"
            " JOIN cash_sales s ON s.id = c.sale_id JOIN users u ON u.id = c.created_by")


def corrections_for_day(db: Database, day: str):
    """Korrekturen, die einen (abgeschlossenen) Tag betreffen."""
    return db.query(_corr_select() + " WHERE c.practice_id = ? AND c.day = ? ORDER BY c.id", (current_practice_id(), day))


def corrections_for_sale(db: Database, sale_id: int):
    return db.query(_corr_select() + " WHERE c.practice_id = ? AND c.sale_id = ? ORDER BY c.id", (current_practice_id(), sale_id))


def refunds_on(db: Database, day: str):
    """Geld, das an diesem Tag für Quittungen abgeschlossener Tage zurückgegeben wurde (zählt in der Kasse dieses Tages)."""
    return db.query(_corr_select() + " WHERE c.practice_id = ? AND c.refund_day = ? ORDER BY c.id", (current_practice_id(), day))


def correct_storno(db: Database, sale, reason: str, *, refund_day: str | None) -> int | None:
    """Quittung eines abgeschlossenen Tages stornieren – als angefügte Korrektur (die Quittung und der Abschluss bleiben).
    refund_day: Tag, an dem das Geld zurückgegeben wird (dieser Tag darf noch nicht abgeschlossen sein); None = Buchungsfehler."""
    reason = (reason or "").strip()[:200]
    if not reason or sale["status"] not in ("paid", "invoiced") or not is_closed(db, sale["day"]):
        return None
    if corrections_for_sale_kind(db, sale["id"], "storno"):
        return None
    if sale["method"] == "invoice":
        refund_day = None                      # auf Rechnung: kein Geld an der Theke
    if refund_day is not None and is_closed(db, refund_day):
        return None
    method = current_method(db, sale)         # nach einer Korrektur der Zahlungsart (z. B. bar → Karte) gilt die neue
    with db.tx():
        cid = db.execute("INSERT INTO cash_corrections (practice_id, day, kind, sale_id, method, amount_chf, refund_day, reason, created_by,"
                         " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (current_practice_id(), sale["day"], "storno", sale["id"], method, sale["total_chf"], refund_day, reason,
                          current_user_id(), utcnow()))
        db.execute("UPDATE subscriptions SET status = 'cancelled' WHERE sale_id = ? AND practice_id = ?", (sale["id"], current_practice_id()))
        audit(db, "correct", "cash_sale", sale["id"], sale["patient_id"],
              {"kind": "storno", "number": sale["number"], "day": sale["day"], "amount": sale["total_chf"], "refund_day": refund_day,
               "method": method, "reason": reason})
    return cid


def correct_method(db: Database, sale, to_method: str, reason: str) -> int | None:
    """Falsche Zahlungsart auf einer Quittung eines abgeschlossenen Tages (z. B. bar statt Karte)."""
    reason = (reason or "").strip()[:200]
    if not reason or to_method not in PAID_METHODS or sale["method"] not in PAID_METHODS or sale["status"] != "paid" \
            or not is_closed(db, sale["day"]) or corrections_for_sale_kind(db, sale["id"], "storno"):
        return None
    current = sale["method"]
    for c in corrections_for_sale_kind(db, sale["id"], "method"):
        current = c["to_method"]
    if to_method == current:
        return None
    cid = db.execute("INSERT INTO cash_corrections (practice_id, day, kind, sale_id, method, to_method, amount_chf, reason, created_by,"
                     " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (current_practice_id(), sale["day"], "method", sale["id"], current, to_method, sale["total_chf"], reason,
                      current_user_id(), utcnow()))
    audit(db, "correct", "cash_sale", sale["id"], sale["patient_id"],
          {"kind": "method", "number": sale["number"], "from": current, "to": to_method, "reason": reason})
    return cid


def corrections_for_sale_kind(db: Database, sale_id: int, kind: str):
    return db.query("SELECT * FROM cash_corrections WHERE practice_id = ? AND sale_id = ? AND kind = ? ORDER BY id",
                    (current_practice_id(), sale_id, kind))


def current_method(db: Database, sale) -> str:
    """Zahlungsart nach allfälligen Korrekturen."""
    m = sale["method"]
    for c in corrections_for_sale_kind(db, sale["id"], "method"):
        m = c["to_method"]
    return m


# --------------------------------------------------------------------------- Abos
def _create_subscription(db: Database, patient_id: int, sale_id: int, item, label: str, day: str) -> int:
    until = (date.fromisoformat(day) + timedelta(days=int(item["abo_days"] or 1) - 1)).isoformat()
    sid = db.execute(
        "INSERT INTO subscriptions (practice_id, patient_id, sale_id, price_item_id, label, sessions_total, valid_from, valid_until,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, sale_id, item["id"], label, item["abo_sessions"], day, until, current_user_id(), utcnow()))
    audit(db, "create", "subscription", sid, patient_id, {"label": label, "until": until})
    return sid


def _with_usage(db: Database, rows, today: str) -> list[dict]:
    out = []
    for r in rows:
        used = db.value("SELECT COUNT(*) FROM subscription_visits WHERE practice_id = ? AND subscription_id = ?",
                        (current_practice_id(), r["id"]), 0)
        last = db.value("SELECT MAX(visited_on) FROM subscription_visits WHERE practice_id = ? AND subscription_id = ?",
                        (current_practice_id(), r["id"]))
        d = dict(r)
        d["used"] = used
        d["last_visit"] = last
        d["left"] = (r["sessions_total"] - used) if r["sessions_total"] else None
        d["visited_today"] = last == today
        if r["status"] == "cancelled":
            d["state"] = "cancelled"
        elif r["valid_until"] < today:
            d["state"] = "expired"
        elif r["valid_from"] > today:
            d["state"] = "future"
        elif r["sessions_total"] and used >= r["sessions_total"]:
            d["state"] = "used_up"
        else:
            d["state"] = "active"
        out.append(d)
    return out


def subscription(db: Database, sub_id: int, today: str):
    row = db.one("SELECT s.*, p.first_name, p.last_name FROM subscriptions s JOIN patients p ON p.id = s.patient_id"
                 " WHERE s.id = ? AND s.practice_id = ?", (sub_id, current_practice_id()))
    return _with_usage(db, [row], today)[0] if row else None


def subscriptions_for(db: Database, patient_id: int, today: str) -> list[dict]:
    rows = db.query("SELECT s.*, p.first_name, p.last_name FROM subscriptions s JOIN patients p ON p.id = s.patient_id"
                    " WHERE s.practice_id = ? AND s.patient_id = ? ORDER BY s.valid_until DESC", (current_practice_id(), patient_id))
    return _with_usage(db, rows, today)


def current_subscriptions(db: Database, today: str, *, recent_days: int = 30) -> list[dict]:
    """Laufende Abos und solche, die in den letzten Tagen abgelaufen sind (zum Verlängern)."""
    since = (date.fromisoformat(today) - timedelta(days=recent_days)).isoformat()
    rows = db.query("SELECT s.*, p.first_name, p.last_name FROM subscriptions s JOIN patients p ON p.id = s.patient_id"
                    " WHERE s.practice_id = ? AND s.status = 'active' AND s.valid_until >= ? ORDER BY p.last_name, p.first_name",
                    (current_practice_id(), since))
    return _with_usage(db, rows, today)


def visits(db: Database, sub_id: int):
    return db.query("SELECT v.*, u.name AS creator_name FROM subscription_visits v LEFT JOIN users u ON u.id = v.created_by"
                    " WHERE v.practice_id = ? AND v.subscription_id = ? ORDER BY v.visited_on DESC", (current_practice_id(), sub_id))


def add_visit(db: Database, sub: dict, day: str) -> bool:
    """Besuch abhaken (höchstens einmal pro Tag). Nur bei gültigem Abo mit freien Besuchen."""
    if sub["state"] != "active" or sub["visited_today"]:
        return False
    vid = db.execute("INSERT INTO subscription_visits (practice_id, subscription_id, visited_on, created_by, created_at)"
                     " VALUES (?,?,?,?,?)", (current_practice_id(), sub["id"], day, current_user_id(), utcnow()))
    audit(db, "visit", "subscription", sub["id"], sub["patient_id"], {"visit": vid, "day": day})
    return True


def remove_visit(db: Database, sub: dict, day: str) -> bool:
    """Irrtümlich abgehakten Besuch von heute zurücknehmen."""
    row = db.one("SELECT id FROM subscription_visits WHERE practice_id = ? AND subscription_id = ? AND visited_on = ?",
                 (current_practice_id(), sub["id"], day))
    if row is None:
        return False
    db.execute("DELETE FROM subscription_visits WHERE id = ? AND practice_id = ?", (row["id"], current_practice_id()))
    audit(db, "visit_undo", "subscription", sub["id"], sub["patient_id"], {"day": day})
    return True


def checkin_visit(db: Database, patient_id: int, day: str) -> dict | None:
    """Check-in der Patientin / des Patienten (z. B. MTT per QR-Code): zählt einen Besuch auf dem ältesten gültigen Abo.
    Höchstens einmal pro Tag. Gibt das Abo (mit Stand) zurück oder None, wenn kein gültiges Abo da ist."""
    subs = [s for s in subscriptions_for(db, patient_id, day) if s["state"] == "active" or s["visited_today"]]
    if not subs:
        return None
    done = next((s for s in subs if s["visited_today"]), None)
    if done:
        return done
    sub = sorted(subs, key=lambda s: s["valid_until"])[0]
    db.execute("INSERT INTO subscription_visits (practice_id, subscription_id, visited_on, created_by, source, created_at)"
               " VALUES (?,?,?,?,?,?)", (current_practice_id(), sub["id"], day, None, "checkin", utcnow()))
    audit(db, "visit", "subscription", sub["id"], patient_id, {"day": day, "source": "checkin"})
    return subscription(db, sub["id"], day)
