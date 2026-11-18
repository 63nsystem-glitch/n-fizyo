"""Öffentliche Schnittstelle der Abrechnung – nur das hier dürfen andere Module benutzen (z. B. Kasse, Export).

Preisliste und MWST lesen, Rechnung aus Preislisten-Positionen anlegen, offene Rechnung an der Theke bezahlen,
Zahlungen eines Tages (für Kassenbuch und Tagesabschluss).
"""
from __future__ import annotations

from aiohttp import web

from ...core import formats, money
from ...core.db import Database
from ...core.tenant import current_practice_id
from . import payment, private, repo

PAYMENT_METHODS = repo.PAYMENT_METHODS
DESK_METHODS = ("cash", "twint", "card")
VAT_RATES = private.VAT_RATES

# Tagesabschluss der Kasse: Die Kasse (Modul «cashdesk», braucht die Abrechnung) meldet hier, wie man prüft, ob ein Kassentag
# abgeschlossen ist: app[CASH_DAY_CLOSED] = fn(db, "JJJJ-MM-TT") → bool. Die Abrechnung kennt die Kasse so nicht und bucht
# trotzdem kein Bargeld, Twint oder Karte auf einen abgeschlossenen Tag (Zahlung, Rückzahlung, Storno auf der Rechnungsseite).
CASH_DAY_CLOSED = web.AppKey("nfizyo_cash_day_closed", object)


def cash_day_closed(app, db: Database, day: str) -> bool:
    """Ist der Kassentag «day» abgeschlossen? False, wenn es keine Kasse gibt."""
    check = app.get(CASH_DAY_CLOSED)
    return bool(check and check(db, day))


def price_items(db: Database, kinds: tuple[str, ...] | None = None):
    """Aktive Positionen der Preisliste (kind: service | product | abo)."""
    return private.items(db, kinds)


def price_item(db: Database, item_id: int | None):
    return private.item(db, item_id)


def item_label(item, lang: str) -> str:
    return private.label(item, lang)


def clean_uid(value: str | None) -> str | None:
    """UID prüfen und einheitlich schreiben («CHE-123.456.788») – sonst None."""
    return private.clean_uid(value)


def vat_number(db: Database) -> str | None:
    """UID der Praxis – None = nicht MWST-pflichtig (dann wird keine MWST verrechnet)."""
    return private.vat_number(db)


def vat_rate(db: Database, item) -> float:
    return private.rate_for(db, item["vat_rate"])


def vat_in(amount: float, rate: float) -> float:
    return private.vat_in(amount, rate)


def vat_summary(lines) -> list[dict]:
    return private.vat_summary(lines)


def create_invoice_from_items(db: Database, patient, entries: list[tuple], lang: str) -> int | None:
    """Rechnungsentwurf (Selbstzahler) aus [(price_item, Anzahl)] – Abschluss wie immer durch die Leitung."""
    return repo.create_from_items(db, patient, entries, lang)


def discard_draft_invoice(db: Database, invoice_id: int | None) -> str:
    """Für den Storno eines Verkaufs «auf Rechnung»: Entwurf löschen. Antwort: "none" (keine), "deleted", "final" (schon
    abgeschlossen – dann zuerst die Rechnung stornieren, nicht still weiterlaufen lassen)."""
    inv = repo.get(db, invoice_id) if invoice_id else None
    if inv is None or inv["status"] == "cancelled":
        return "none"
    if inv["status"] != "draft":
        return "final"
    repo.delete_draft(db, inv)
    return "deleted"


def open_invoice_by_number(db: Database, number: str):
    """Abgeschlossene Rechnung mit offenem Betrag: {"id","number","patient_id","name","open","cash_due","total"} oder None.
    cash_due = offener Betrag bar (auf 5 Rappen gerundet)."""
    inv = db.one("SELECT id FROM invoices WHERE practice_id = ? AND number = ? AND status = 'final'",
                 (current_practice_id(), (number or "").strip()))
    if inv is None:
        return None
    full = repo.get(db, inv["id"])
    b = repo.balance(db, full)
    if b["open"] <= repo.PAID_TOLERANCE:
        return None
    return {"id": full["id"], "number": full["number"], "patient_id": full["patient_id"],
            "name": f"{full['first_name']} {full['last_name']}", "open": b["open"], "cash_due": money.cash(b["open"]),
            "total": full["total_chf"]}


def pay_at_desk(db: Database, invoice_id: int, amount: float, method: str, day: str) -> int | None:
    """Zahlung an der Theke (bar, Twint, Karte) auf eine abgeschlossene Rechnung – höchstens der offene Betrag.
    Bar auf 5 Rappen: CHF 186.48 offen → 186.50 bar, die Rechnung ist bezahlt, +0.02 Rundung (core/money.py)."""
    inv = repo.get(db, invoice_id)
    if inv is None or inv["status"] != "final" or method not in DESK_METHODS or not (0 < amount <= 100000):
        return None
    open_ = repo.balance(db, inv)["open"]
    given = min(money.cash(amount), money.cash(open_)) if method == "cash" else min(money.rp(amount), open_)
    credited, rounding = repo.cash_amounts(open_, given, method)
    if credited <= 0:
        return None
    return repo.add_payment(db, amount=credited, booked_on=day, source="manual", invoice_id=invoice_id, method=method, rounding=rounding)


def cash_due(amount: float, method: str = "cash") -> float:
    """Was an der Theke zu bezahlen ist: bar auf 5 Rappen gerundet, sonst genau."""
    return money.cash(amount) if method == "cash" else money.rp(amount)


def desk_payments(db: Database, day_from: str, day_to: str | None = None):
    """Zahlungen auf Rechnungen mit bar/Twint/Karte an diesen Tagen (Kassenbuch). amount_chf = der Rechnung gutgeschrieben,
    rounding_chf = Rundung auf 5 Rappen (bar), cash_chf = was wirklich in die Kasse ging (amount_chf + rounding_chf)."""
    return db.query(
        "SELECT pm.id, pm.amount_chf, pm.rounding_chf, ROUND(pm.amount_chf + pm.rounding_chf, 2) AS cash_chf, pm.booked_on, pm.method,"
        " pm.kind, pm.created_at, i.number, i.patient_id"
        " FROM invoice_payments pm JOIN invoices i ON i.id = pm.invoice_id"
        " WHERE pm.practice_id = ? AND pm.method != 'bank' AND pm.booked_on BETWEEN ? AND ? ORDER BY pm.booked_on, pm.id",
        (current_practice_id(), day_from, day_to or day_from))


# --------------------------------------------------------------------------- Buchhaltung (Modul «exports»)
def _split(db: Database, invoice_id: int) -> dict:
    """Ertrag ohne MWST je Art (Leistung / Produkt) und MWST einer Rechnung."""
    out = {"services": 0.0, "products": 0.0, "vat": 0.0}
    for ln in db.query("SELECT l.amount_chf, l.vat_rate, p.kind FROM invoice_lines l LEFT JOIN price_items p ON p.id = l.price_item_id"
                       " WHERE l.practice_id = ? AND l.invoice_id = ?", (current_practice_id(), invoice_id)):
        vat = private.vat_in(ln["amount_chf"], ln["vat_rate"] or 0)
        out["vat"] += vat
        out["products" if ln["kind"] == "product" else "services"] += ln["amount_chf"] - vat
    return {k: money.rp(v) for k, v in out.items()}


def accounting_invoices(db: Database, start_utc: str, end_utc: str) -> list[dict]:
    """Im Zeitraum abgeschlossene Rechnungen (auch später stornierte) mit Aufteilung des Ertrags."""
    rows = db.query("SELECT i.id, i.number, i.finalized_at, i.law, i.tiers, i.tariff, i.total_chf, i.vat_chf, i.status, i.cancelled_at,"
                    " p.first_name, p.last_name, ins.name AS insurer_name FROM invoices i JOIN patients p ON p.id = i.patient_id"
                    " LEFT JOIN insurers ins ON ins.id = i.insurer_id WHERE i.practice_id = ? AND i.imported_from IS NULL"
                    " AND i.finalized_at >= ? AND i.finalized_at < ?"
                    " ORDER BY i.finalized_at, i.id", (current_practice_id(), start_utc, end_utc))
    return [dict(r) | _split(db, r["id"]) for r in rows]


def accounting_cancellations(db: Database, start_utc: str, end_utc: str) -> list[dict]:
    rows = db.query("SELECT i.id, i.number, i.cancelled_at, i.total_chf, i.cancel_reason, p.first_name, p.last_name FROM invoices i"
                    " JOIN patients p ON p.id = i.patient_id WHERE i.practice_id = ? AND i.status = 'cancelled' AND i.finalized_at IS NOT NULL"
                    " AND i.imported_from IS NULL"
                    " AND i.cancelled_at >= ? AND i.cancelled_at < ? ORDER BY i.cancelled_at", (current_practice_id(), start_utc, end_utc))
    return [dict(r) | _split(db, r["id"]) for r in rows]


def accounting_payments(db: Database, day_from: str, day_to: str) -> list[dict]:
    """Zahlungen im Zeitraum (auch Rückzahlungen und Stornos: Betrag negativ). number = Rechnung heute;
    booked_number = Rechnung beim Buchen (vor einer späteren Umbuchung, siehe accounting_payment_moves)."""
    rows = [dict(r) for r in db.query(
        "SELECT pm.id, pm.booked_on, pm.amount_chf, pm.rounding_chf, pm.method, pm.source, pm.debtor, pm.reference, pm.kind,"
        " pm.reversal_of, pm.note, pm.message,"
        " i.number, (SELECT fi.number FROM payment_moves m LEFT JOIN invoices fi ON fi.id = m.from_invoice_id WHERE m.payment_id = pm.id"
        " ORDER BY m.id LIMIT 1) AS first_from, (SELECT COUNT(*) FROM payment_moves m WHERE m.payment_id = pm.id) AS moves"
        " FROM invoice_payments pm LEFT JOIN invoices i ON i.id = pm.invoice_id WHERE pm.practice_id = ? AND pm.booked_on BETWEEN ? AND ?"
        " ORDER BY pm.booked_on, pm.id", (current_practice_id(), day_from, day_to))]
    for r in rows:
        r["booked_number"] = r.pop("first_from") if r.pop("moves") else r["number"]
    return rows


def accounting_payment_moves(db: Database, day_from: str, day_to: str) -> list[dict]:
    """Geänderte Zuordnungen im Zeitraum (Umbuchung zwischen Debitoren und «ungeklärt» am Tag der Änderung)."""
    return [dict(r) for r in db.query(
        "SELECT m.id, m.payment_id, m.moved_on, pm.amount_chf, pm.method, fi.number AS from_number, ti.number AS to_number"
        " FROM payment_moves m JOIN invoice_payments pm ON pm.id = m.payment_id LEFT JOIN invoices fi ON fi.id = m.from_invoice_id"
        " LEFT JOIN invoices ti ON ti.id = m.to_invoice_id WHERE m.practice_id = ? AND m.moved_on BETWEEN ? AND ? ORDER BY m.moved_on, m.id",
        (current_practice_id(), day_from, day_to))]


def accounting_fees(db: Database, start_utc: str, end_utc: str) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT r.id, r.level, r.fee_chf, r.created_at, i.number FROM invoice_reminders r JOIN invoices i ON i.id = r.invoice_id"
        " WHERE r.practice_id = ? AND r.fee_chf > 0 AND r.created_at >= ? AND r.created_at < ? ORDER BY r.created_at",
        (current_practice_id(), start_utc, end_utc))]


def open_items(db: Database) -> list[dict]:
    """Offene Posten heute: Nummer, Datum, fällig, Patient/in, Betrag offen."""
    return [{"number": x["inv"]["number"], "finalized_at": x["inv"]["finalized_at"], "due_on": x["inv"]["due_on"],
             "name": f"{x['inv']['first_name']} {x['inv']['last_name']}", "total": x["inv"]["total_chf"], "open": x["b"]["open"],
             "state": x["b"]["state"], "level": x["level"]} for x in repo.open_items(db)]


def cost_approval(db: Database, patient_id: int, on: str):
    """Neueste Kostengutsprache der Patientin / des Patienten: {"status", "requested_on", "valid_until", "sessions_approved",
    "valid"} – oder None. valid = genehmigt (ganz/teilweise) und am Tag «on» noch gültig."""
    row = db.one("SELECT status, requested_on, valid_until, sessions_approved FROM cost_approvals WHERE practice_id = ? AND patient_id = ?"
                 " ORDER BY id DESC LIMIT 1", (current_practice_id(), patient_id))
    if row is None:
        return None
    d = dict(row)
    d["valid"] = d["status"] in ("approved", "partial") and (not d["valid_until"] or d["valid_until"] >= on)
    return d



# --------------------------------------------------------------------------- Zahlteil für Rechnungen anderer Module (Firmenkunden)
def payment_settings(db: Database) -> dict:
    """IBAN, Zahlungsfrist, Mahnfrist und Mahngebühren der Praxis (Leitung › Einstellungen › Zahlung)."""
    return payment.settings(db)


def payment_iban(db: Database) -> str | None:
    """Gültige IBAN / QR-IBAN der Praxis oder None."""
    iban = payment.clean_iban(payment.settings(db)["iban"])
    return iban if iban and payment.valid_iban(iban) else None


def payment_problems(db: Database) -> list[str]:
    """Was für den QR-Zahlteil fehlt: missing_iban, missing_practice_address."""
    return repo.payment_problems(db)


def practice(db: Database):
    return repo.practice_row(db)


def reference(iban: str, kind_digit: int, prefix: str, obj_id: int) -> str:
    """Zahlungsreferenz für Belege anderer Module, getrennt von den Rechnungen der Abrechnung:
    QR-IBAN → QR-Referenz «<Kennziffer><Nummer, 25-stellig><Prüfziffer>»; sonst Creditor Reference «RF..<prefix><Nummer>»."""
    if payment.is_qr_iban(iban):
        body = f"{kind_digit}{obj_id:025d}"
        return body + str(payment.mod10(body))
    return payment.scor(f"{prefix}{obj_id}")


def parse_reference(ref: str | None, kind_digit: int, prefix: str) -> int | None:
    """Gegenstück zu reference(): Nummer des Belegs oder None (Prüfziffer muss stimmen)."""
    import re

    s = re.sub(r"\s", "", ref or "").upper()
    if re.fullmatch(r"[0-9]{27}", s) and s[0] == str(kind_digit) and payment.mod10(s[:26]) == int(s[26]):
        return int(s[1:26]) or None
    m = re.fullmatch(r"RF[0-9]{2}" + re.escape(prefix.upper()) + r"([0-9]{1,18})", s)
    if m and payment.scor(f"{prefix}{int(m.group(1))}") == s:
        return int(m.group(1))
    return None


def address_lines(name: str, street: str | None, house: str | None, zip_code: str | None, city: str | None) -> list[str]:
    from .pdf import _addr_lines

    return _addr_lines(name, street, house, zip_code, city)


def chf(value: float) -> str:
    """1234.5 → «1’234.50» (wie auf den Rechnungen der Abrechnung)."""
    from .pdf import _chf

    return _chf(value)


def qr_flowable(*, practice, iban: str, amount: float, reference: str, debtor: dict | None, info: str, lang: str):
    """QR-Zahlteil als reportlab-Flowable (unten auf die aktuelle Seite) – oder None, wenn die Adresse der Praxis fehlt.
    debtor = {"name", "street", "house_no", "zip", "city"} oder None."""
    from ...core import pdf as core_pdf
    from .pdf import QRBillFlowable, _addr_lines, _chf

    creditor = payment.address(practice["name"], practice["street"], practice["house_no"], practice["zip"], practice["city"])
    if creditor is None:
        return None
    deb = payment.address(debtor["name"], debtor.get("street"), debtor.get("house_no"), debtor.get("zip"), debtor.get("city")) if debtor else None
    bill = payment.qr_bill(iban=iban, creditor=creditor, debtor=deb, amount=amount, reference=reference, info=info, lang=lang)
    return QRBillFlowable({
        "qr_data": bill.qr_data(), "account": core_pdf.format_iban(iban),
        "creditor_lines": _addr_lines(practice["name"], practice["street"], practice["house_no"], practice["zip"], practice["city"]),
        "reference": core_pdf.format_reference(reference), "info": info,
        "debtor_lines": _addr_lines(debtor["name"], debtor.get("street"), debtor.get("house_no"), debtor.get("zip"), debtor.get("city")) if deb else None,
        "amount": _chf(amount).replace("’", " "), "lang": lang if lang in ("de", "fr", "it") else "de"})


# --------------------------------------------------------------------------- Einrichtung und Datenübernahme (Module «settings», «dataimport»)
def kvg_tp_value(db: Database) -> float | None:
    """KVG-Taxpunktwert der Praxis für den heute gültigen Tarif – None, solange er nicht eingetragen ist."""
    from ...core.timeutil import today
    from ...knowledge import tariffs as catalog

    t = catalog.tariff_for_law("KVG", on=today().isoformat())
    return repo.tp_value(db, t.key if t else "kvg312", today().isoformat())


def imported_exists(db: Database, old_number: str) -> bool:
    """Wurde dieser offene Posten (Nummer im alten System) schon übernommen?"""
    return bool(db.value("SELECT 1 FROM invoices WHERE practice_id = ? AND imported_from = ?",
                         (current_practice_id(), (old_number or "").strip())))


def import_open_item(db: Database, patient, *, old_number: str, invoice_date: str, amount: float, lang: str,
                     due_on: str | None = None, reminder_level: int = 0, original_total: float | None = None) -> int | None:
    """Offener Posten aus der bisherigen Software → abgeschlossene Rechnung über den OFFENEN Betrag (Selbstzahler-Art, QR-Zahlteil),
    Nummer «V-<alte Nummer>». Bereits verschickte Mahnstufen werden übernommen (ohne neue Gebühr), damit die nächste Stufe stimmt.
    Erscheint unter «Offene Posten», kann gemahnt und bezahlt werden (auch über die Bankdatei), aber nicht im Buchhaltungs-Export
    (dort ist der Ertrag schon im alten System gebucht). None, wenn schon übernommen oder der Betrag ungültig ist."""
    from datetime import date as _date
    from datetime import timedelta

    from ...core.audit import audit
    from ...core.db import utcnow
    from ...core.tenant import current_user_id
    from ...core.timeutil import fmt_date, today

    old_number = (old_number or "").strip()[:30]
    if not old_number or not (0 < amount <= 1_000_000) or imported_exists(db, old_number):
        return None
    st = payment.settings(db)
    iban = payment.clean_iban(st["iban"])
    issued = _date.fromisoformat(invoice_date)
    due = due_on or (issued + timedelta(days=int(st["payment_days"]))).isoformat()
    label = {"fr": "Poste ouvert repris de l'ancien logiciel : facture {n} du {d}",
             "it": "Partita aperta dal software precedente: fattura {n} del {d}"}.get(
        lang, "Offener Posten aus der bisherigen Software: Rechnung {n} vom {d}").format(n=old_number, d=fmt_date(issued, lang))
    if original_total and abs(original_total - amount) > repo.PAID_TOLERANCE:
        label += f" ({'total' if lang == 'fr' else 'ursprünglich'} {formats.chf(original_total)})"
    law = patient["law"] if patient["law"] in private.PRIVATE_LAWS else "privat"
    with db.tx():
        iid = db.execute(
            "INSERT INTO invoices (practice_id, patient_id, law, tariff, tiers, created_by, created_at, imported_from)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (current_practice_id(), patient["id"], law, private.PRIVATE, "garant", current_user_id(), utcnow(), old_number))
        repo._insert_line(db, iid, {"service_date": issued.isoformat(), "therapist_id": None, "code": None, "label": label[:200],
                                    "quantity": 1.0, "unit_price": round(amount, 2), "vat_rate": 0.0, "amount_chf": round(amount, 2)})
        repo.recalc(db, iid)
        db.execute("UPDATE invoices SET status = 'final', number = ?, finalized_at = ?, iban = ?, qr_reference = ?, due_on = ?"
                   " WHERE id = ? AND practice_id = ?",
                   (f"V-{old_number}", f"{issued.isoformat()}T11:00:00+00:00", iban or None,
                    payment.reference_for(iban, iid) if iban else None, due, iid, current_practice_id()))
        next_due = (today() + timedelta(days=int(st["reminder_days"]))).isoformat()
        for level in range(1, max(0, min(int(reminder_level or 0), 3)) + 1):
            db.execute("INSERT INTO invoice_reminders (practice_id, invoice_id, level, fee_chf, open_chf, due_on, created_by, created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)", (current_practice_id(), iid, level, 0.0, round(amount, 2), next_due,
                                                     current_user_id(), utcnow()))
        audit(db, "import", "invoice", iid, patient["id"], {"old_number": old_number, "open": round(amount, 2),
                                                            "reminders": int(reminder_level or 0)})
    return iid


# --------------------------------------------------------------------------- MediData-Teilnehmerverzeichnis (für Einstellungen › Versicherer)
def medidata_directory(db: Database) -> dict:
    """{count, fetched_at} des gespeicherten MediData-Teilnehmerverzeichnisses dieser Praxis."""
    from . import medidata_sync

    return medidata_sync.directory_info(db)


def medidata_reach(db: Database, glns) -> dict:
    """GLN → Erreichbarkeit laut Verzeichnis: {listed, receiver, name, v50, v45, tg_allowed, additional_costs, laws}.
    Leeres Ergebnis, solange das Verzeichnis nicht geladen ist."""
    from . import medidata_sync

    return {g: r for g in {g for g in glns if g} if (r := medidata_sync.reach(db, g)) is not None}


def medidata_search(db: Database, q: str, limit: int = 20) -> list[dict]:
    """Teilnehmer nach Name(steil) oder GLN suchen."""
    import json

    from .medidata import LAW_CODES, accepts

    q = (q or "").strip()
    if len(q) < 2:
        return []
    rows = db.query("SELECT * FROM medidata_participants WHERE practice_id = ? AND (gln_participant = ? OR lower(name) LIKE ?)"
                    " ORDER BY name LIMIT ?", (current_practice_id(), q, f"%{q.lower()}%", limit))
    out = []
    for r in rows:
        ns = json.loads(r["not_supported"] or "[]")
        laws = [LAW_CODES[x] for x in json.loads(r["law_types"] or "[]") if x in LAW_CODES]
        out.append({"gln": r["gln_participant"], "receiver": r["gln_receiver"], "name": r["name"], "street": r["street"], "zip": r["zip"],
                    "town": r["town"], "laws": laws, "v50": accepts(ns, "5.0"), "v45": accepts(ns, "4.5"), "tg_allowed": bool(r["tg_allowed"])})
    return out


def medidata_participant(db: Database, gln: str):
    """Ein Eintrag (nach glnParticipant) – zum Übernehmen als Versicherer."""
    return db.one("SELECT * FROM medidata_participants WHERE practice_id = ? AND gln_participant = ? LIMIT 1", (current_practice_id(), gln))
