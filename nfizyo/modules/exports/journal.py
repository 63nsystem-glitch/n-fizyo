"""Buchungsjournal (doppelte Buchhaltung) aus Abrechnung und Kasse – für die Treuhand oder den Import in Banana, bexio,
Abacus usw. (Spalten bei Bedarf in der Import-Vorlage der Software zuordnen).

Buchungen:
  Rechnung abgeschlossen   Debitoren / Ertrag Leistungen, Ertrag Produkte, geschuldete MWST
  Rechnung storniert       Gegenbuchung (Ertrag … / Debitoren)
  Mahngebühr               Debitoren / Übriger Ertrag
  Zahlung (Bank, bar, Twint/Karte)   Bank | Kasse | Durchlaufkonto / Debitoren (nicht zugeordnet: / Konto «ungeklärt»)
  Rückzahlung, Storno einer Zahlung  Gegenbuchung (Debitoren | ungeklärt / Bank | Kasse …) am Tag der Korrektur
  Zuordnung geändert       Umbuchung ungeklärt / Debitoren (zugeordnet) bzw. Debitoren / ungeklärt (aufgehoben) am Tag der Änderung
  Kasse: Korrektur nach Abschluss    Storno: Gegenbuchung des Verkaufs; Zahlungsart: Umbuchung zwischen Kasse und Durchlaufkonto
  Verkauf an der Kasse     Kasse | Durchlaufkonto / Ertrag Produkte, Ertrag Leistungen, geschuldete MWST
  Firmenrechnungen         wie Rechnungen (Debitoren / Ertrag …), Zahlungen, Mahngebühren und Stornos ebenso
  Rundung 5 Rappen (bar)   Kasse / Rundungsdifferenzen (bzw. umgekehrt) – Barzahlungen und Barverkäufe (core/money.py)

Jede Zeile hat eine feste Herkunft (Entry.src, nur IDs): Grundlage dafür, dass bexio nichts doppelt bekommt (bexio.entry_key).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ...core import money as core_money
from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.spreadsheet import safe_row
from ...core.tenant import current_practice_id
from ...core.timeutil import TZ, from_iso, to_utc_iso
from ..billing import api as billing_api

ACCOUNTS = ("acc_receivable", "acc_bank", "acc_cash", "acc_transit", "acc_services", "acc_products", "acc_fees", "acc_vat", "acc_unassigned",
            "acc_rounding")
DEFAULTS = {"acc_receivable": "1100", "acc_bank": "1020", "acc_cash": "1000", "acc_transit": "1091", "acc_services": "3400",
            "acc_products": "3200", "acc_fees": "3600", "acc_vat": "2200", "acc_unassigned": "1100", "acc_rounding": ""}


def stored_settings(db: Database) -> dict:
    """Kontenplan wie gespeichert (acc_rounding leer = folgt «Ertrag Leistungen»)."""
    row = db.one("SELECT * FROM export_settings WHERE practice_id = ?", (current_practice_id(),))
    return {**DEFAULTS, **({k: row[k] for k in ACCOUNTS if row[k]} if row else {})}


def settings(db: Database) -> dict:
    """Kontenplan der Praxis. acc_rounding (Rundungsdifferenzen bar) leer = wie «Ertrag Leistungen»."""
    out = stored_settings(db)
    out["acc_rounding"] = out["acc_rounding"] or out["acc_services"]
    return out


def save_settings(db: Database, values: dict) -> None:
    cur = stored_settings(db)
    cur.update({k: v for k, v in values.items() if k in ACCOUNTS and v})
    if "acc_rounding" in values:
        cur["acc_rounding"] = values["acc_rounding"] or None         # leer = wie Ertrag Leistungen
    db.execute(f"INSERT INTO export_settings (practice_id, {', '.join(ACCOUNTS)}, updated_at) VALUES (?, {', '.join('?' * len(ACCOUNTS))}, ?)"
               f" ON CONFLICT(practice_id) DO UPDATE SET {', '.join(f'{k} = excluded.{k}' for k in ACCOUNTS)}, updated_at = excluded.updated_at",
               (current_practice_id(), *[cur[k] for k in ACCOUNTS], utcnow()))
    audit(db, "update", "export_settings", current_practice_id(), None, {k: cur[k] for k in ACCOUNTS})


@dataclass
class Entry:
    day: str
    doc: str
    debit: str
    credit: str
    amount: float
    text: str
    vat_rate: str = ""
    src: str = ""          # Herkunft (nur IDs, z. B. «pay:17», «inv:12:vat») – fester Schlüssel für bexio, nie Namen/Text


def _local_day(iso: str) -> str:
    return from_iso(iso).date().isoformat()


def bounds(day_from: date, day_to: date) -> tuple[str, str]:
    start = datetime(day_from.year, day_from.month, day_from.day, tzinfo=TZ)
    end = datetime(day_to.year, day_to.month, day_to.day, tzinfo=TZ) + timedelta(days=1)
    return to_utc_iso(start), to_utc_iso(end)


def _revenue(entries: list[Entry], day: str, doc: str, debit: str, acc: dict, row: dict, total: float, text: str, *, reverse=False,
             src: str = "") -> None:
    """Gesamtbetrag auf Ertrag (Leistungen/Produkte) und MWST verteilen; Rundungsdifferenz (Rappen) geht auf Leistungen."""
    parts = [("products", acc["acc_products"], row.get("products") or 0.0), ("vat", acc["acc_vat"], row.get("vat") or 0.0)]
    services = core_money.rp(total - sum(a for _, _, a in parts))
    for part, credit, amount in [("services", acc["acc_services"], services)] + parts:
        if abs(amount) < 0.005:
            continue
        d, c = (credit, debit) if reverse else (debit, credit)
        entries.append(Entry(day, doc, d, c, core_money.rp(amount), text, "MWST" if part == "vat" else "", f"{src}:{part}" if src else ""))


def _money(entries: list[Entry], day: str, doc: str, debit: str, credit: str, amount: float, text: str, src: str = "") -> None:
    """Geldbuchung; negativer Betrag (Rückzahlung, Storno) = Gegenbuchung mit positivem Betrag."""
    if abs(amount) < 0.005 or debit == credit:
        return
    if amount < 0:
        debit, credit, amount = credit, debit, -amount
    entries.append(Entry(day, doc, debit, credit, core_money.rp(amount), text, "", src))


def _rounding(entries: list[Entry], day: str, doc: str, cash_acc: str, acc: dict, amount: float, label: str, src: str) -> None:
    """Rundung auf 5 Rappen (bar): positiv = mehr Bargeld als Betrag → Kasse / Rundungsdifferenzen; negativ umgekehrt."""
    _money(entries, day, doc, cash_acc, acc["acc_rounding"], amount, f"Rundung 5 Rappen {label}", src)


def build(db: Database, day_from: date, day_to: date, *, cash_sales: list[dict] | None = None, firms: dict | None = None,
          cash_corrections: list[dict] | None = None) -> dict:
    acc = settings(db)
    lo, hi = bounds(day_from, day_to)
    d0, d1 = day_from.isoformat(), day_to.isoformat()
    invoices = billing_api.accounting_invoices(db, lo, hi)
    cancellations = billing_api.accounting_cancellations(db, lo, hi)
    payments = billing_api.accounting_payments(db, d0, d1)
    fees = billing_api.accounting_fees(db, lo, hi)
    sales = cash_sales or []
    entries: list[Entry] = []
    for inv in invoices:
        _revenue(entries, _local_day(inv["finalized_at"]), inv["number"], acc["acc_receivable"], acc, inv, inv["total_chf"],
                 f"Rechnung {inv['number']} {inv['last_name']} {inv['first_name']} ({inv['law']})", src=f"inv:{inv['id']}")
    for inv in cancellations:
        _revenue(entries, _local_day(inv["cancelled_at"]), inv["number"], acc["acc_receivable"], acc, inv, inv["total_chf"],
                 f"Storno Rechnung {inv['number']} {inv['last_name']} {inv['first_name']}", reverse=True, src=f"inv-storno:{inv['id']}")
    for f in fees:
        entries.append(Entry(_local_day(f["created_at"]), f"{f['number']}-M{f['level']}", acc["acc_receivable"], acc["acc_fees"], f["fee_chf"],
                             f"Mahngebühr Stufe {f['level']} Rechnung {f['number']}", "", f"fee:{f['id']}"))
    money = {"bank": acc["acc_bank"], "cash": acc["acc_cash"], "twint": acc["acc_transit"], "card": acc["acc_transit"]}
    for p in payments:
        number = p.get("booked_number", p["number"])           # Rechnung beim Buchen (spätere Umbuchung: siehe unten)
        credit = acc["acc_receivable"] if number else acc["acc_unassigned"]
        kind = p.get("kind") or "payment"
        if kind == "refund":
            text = f"Rückzahlung Rechnung {number}" if number else "Rückzahlung"
        elif kind == "reversal":
            text = f"Storno Zahlung Z{p['reversal_of']}" + (f" Rechnung {number}" if number else " nicht zugeordnet")
        else:
            text = f"Zahlung Rechnung {number}" if number else f"Zahlung nicht zugeordnet {p['debtor'] or ''} {p['reference'] or ''}".strip()
        cash_acc = money.get(p["method"], acc["acc_bank"])
        _money(entries, p["booked_on"], number or f"Z{p['id']}", cash_acc, credit, p["amount_chf"], f"{text} ({p['method']})", f"pay:{p['id']}")
        if p.get("rounding_chf"):
            _rounding(entries, p["booked_on"], number or f"Z{p['id']}", cash_acc, acc, p["rounding_chf"],
                      f"Zahlung Z{p['id']}" + (f" Rechnung {number}" if number else ""), f"pay:{p['id']}:rounding")
    moves = billing_api.accounting_payment_moves(db, d0, d1)
    for m in moves:
        acc_from = acc["acc_receivable"] if m["from_number"] else acc["acc_unassigned"]
        acc_to = acc["acc_receivable"] if m["to_number"] else acc["acc_unassigned"]
        text = (f"Zuordnung Zahlung Z{m['payment_id']} Rechnung {m['to_number']}" if m["to_number"] and not m["from_number"]
                else f"Zuordnung aufgehoben Zahlung Z{m['payment_id']} Rechnung {m['from_number']}" if m["from_number"] and not m["to_number"]
                else f"Umbuchung Zahlung Z{m['payment_id']} {m['from_number']} → {m['to_number']}")
        # Zahlung lag auf «acc_from» (Haben) → jetzt auf «acc_to»: Soll acc_from, Haben acc_to
        _money(entries, m["moved_on"], f"Z{m['payment_id']}-U{m['id']}", acc_from, acc_to, m["amount_chf"], text, f"move:{m['id']}")
    for s in sales:
        cash_acc = money.get(s["method"], acc["acc_cash"])
        rounding = s.get("rounding") or 0.0                     # bar: im Total enthalten, eigenes Konto
        _revenue(entries, s["day"], s["number"], cash_acc, acc, s, core_money.rp(s["total_chf"] - rounding),
                 f"Kasse {s['number']} ({s['method']})", src=f"sale:{s['id']}")
        if rounding:
            _rounding(entries, s["day"], s["number"], cash_acc, acc, rounding, f"Kasse {s['number']}", f"sale:{s['id']}:rounding")
    corrections = cash_corrections or []
    for c in corrections:
        doc = f"{c['number']}-K{c['id']}"
        if c["kind"] == "storno":
            cash_acc = money.get(c["method"], acc["acc_cash"])
            rounding = c.get("rounding") or 0.0
            _revenue(entries, c["booked_on"], doc, cash_acc, acc, c, core_money.rp(c["amount_chf"] - rounding),
                     f"Storno Kasse {c['number']} nach Abschluss ({c['method']})", reverse=True, src=f"cashcorr:{c['id']}")
            if rounding:
                _rounding(entries, c["booked_on"], doc, cash_acc, acc, -rounding, f"Storno Kasse {c['number']}", f"cashcorr:{c['id']}:rounding")
        else:
            _money(entries, c["booked_on"], doc, money.get(c["to_method"], acc["acc_cash"]), money.get(c["method"], acc["acc_cash"]),
                   c["amount_chf"], f"Kasse {c['number']}: Zahlungsart {c['method']} → {c['to_method']}", f"cashcorr:{c['id']}")
    fm = firms or {"invoices": [], "cancellations": [], "payments": [], "fees": []}
    for inv in fm["invoices"]:
        _revenue(entries, _local_day(inv["finalized_at"]), inv["number"], acc["acc_receivable"], acc, inv, inv["total_chf"],
                 f"Rechnung {inv['number']} {inv['firm_name']} (Firma)", src=f"firm-inv:{inv['id']}")
    for inv in fm["cancellations"]:
        _revenue(entries, _local_day(inv["cancelled_at"]), inv["number"], acc["acc_receivable"], acc, inv, inv["total_chf"],
                 f"Storno Rechnung {inv['number']} {inv['firm_name']} (Firma)", reverse=True, src=f"firm-storno:{inv['id']}")
    for f in fm["fees"]:
        entries.append(Entry(_local_day(f["created_at"]), f"{f['number']}-M{f['level']}", acc["acc_receivable"], acc["acc_fees"], f["fee_chf"],
                             f"Mahngebühr Stufe {f['level']} Rechnung {f['number']}", "", f"firm-fee:{f['id']}"))
    for p in fm["payments"]:
        entries.append(Entry(p["booked_on"], p["number"], money.get(p["method"], acc["acc_bank"]), acc["acc_receivable"], p["amount_chf"],
                             f"Zahlung Rechnung {p['number']} {p['firm_name']} ({p['method']})", "", f"firm-pay:{p['id']}"))
    entries.sort(key=lambda e: (e.day, e.doc))
    all_inv = invoices + fm["invoices"]
    all_pay = payments + fm["payments"]
    all_canc = cancellations + fm["cancellations"]
    # Kasse wie im Journal: Verkäufe des Zeitraums minus Stornos nach dem Tagesabschluss, die im Zeitraum gebucht wurden (F19)
    stornos = [c for c in corrections if c["kind"] == "storno"]
    sales_sum = core_money.total(s["total_chf"] for s in sales)
    storno_sum = core_money.total(c["amount_chf"] for c in stornos)
    return {"entries": entries, "invoices": invoices, "cancellations": cancellations, "payments": payments, "fees": fees, "sales": sales,
            "firms": fm, "accounts": acc, "from": d0, "to": d1, "moves": moves, "cash_corrections": corrections,
            "sums": {"invoices": core_money.total(i["total_chf"] for i in all_inv), "payments": core_money.total(p["amount_chf"] for p in all_pay),
                     "sales": sales_sum, "sales_storno": storno_sum, "sales_storno_n": len(stornos),
                     "sales_net": core_money.rp(sales_sum - storno_sum),
                     "cash_corrections_n": len(corrections),
                     "cancellations": core_money.total(c["total_chf"] for c in all_canc)}}


def _de(d: str) -> str:
    return date.fromisoformat(d).strftime("%d.%m.%Y")


def _corr_text(corrections: list[dict]) -> str:
    """Korrekturen nach dem Tagesabschluss einer Quittung, kurz: «Storno 12.11.2026 (Bar)», «Zahlungsart card → twint 12.11.2026»."""
    out = []
    for c in corrections:
        if c["kind"] == "storno":
            out.append(f"Storno {_de(c['booked_on'])} ({c['method']}): {c['reason']}")
        else:
            out.append(f"Zahlungsart {c['method']} → {c['to_method']} {_de(c['booked_on'])}: {c['reason']}")
    return "; ".join(out)


def _net(sale: dict) -> float:
    """Betrag der Quittung nach einem Storno nach dem Abschluss (sonst das Total)."""
    return 0.0 if any(c["kind"] == "storno" for c in sale.get("corrections") or []) else sale["total_chf"]


def journal_csv(data: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Datum", "Beleg", "Soll", "Haben", "Betrag", "Text", "MWST"])
    for e in data["entries"]:   # Texte entschärft (=, +, -, @ am Anfang → '), Beträge bleiben Zahlen
        w.writerow(safe_row([_de(e.day), e.doc, e.debit, e.credit, f"{e.amount:.2f}", e.text, e.vat_rate]))
    return ("﻿" + buf.getvalue()).encode("utf-8")


def workbook(data: dict, open_items: list[dict], closings: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    sheets = [
        ("Journal", ["Datum", "Beleg", "Soll", "Haben", "Betrag", "Text", "MWST"],
         [[_de(e.day), e.doc, e.debit, e.credit, e.amount, e.text, e.vat_rate] for e in data["entries"]]),
        ("Rechnungen", ["Nummer", "Datum", "Name", "Vorname", "Gesetz", "Versicherer", "Tiers", "Total", "davon Leistungen", "davon Produkte",
                        "MWST", "Status"],
         [[i["number"], _de(_local_day(i["finalized_at"])), i["last_name"], i["first_name"], i["law"], i["insurer_name"] or "", i["tiers"],
           i["total_chf"], i["services"], i["products"], i["vat"], i["status"]] for i in data["invoices"]]),
        ("Firmenrechnungen", ["Nummer", "Datum", "Firma", "Total", "davon Leistungen", "davon Produkte", "MWST", "Status"],
         [[i["number"], _de(_local_day(i["finalized_at"])), i["firm_name"], i["total_chf"], i["services"], i["products"], i["vat"], i["status"]]
          for i in data.get("firms", {}).get("invoices", [])]),
        ("Stornos", ["Nummer", "Datum", "Name", "Vorname", "Total", "Grund"],
         [[c["number"], _de(_local_day(c["cancelled_at"])), c["last_name"], c["first_name"], c["total_chf"], c["cancel_reason"] or ""]
          for c in data["cancellations"]]),
        ("Zahlungen", ["Datum", "Rechnung", "Betrag", "Zahlungsart", "Quelle", "Auftraggeber", "Referenz", "Art", "Rundung bar", "Mitteilung"],
         [[_de(p["booked_on"]), p["number"] or "", p["amount_chf"], p["method"], p["source"], p["debtor"] or "", p["reference"] or "",
           p.get("kind") or "payment", p.get("rounding_chf") or 0.0, p.get("message") or ""]
          for p in data["payments"] + data.get("firms", {}).get("payments", [])]),
        ("Kasse", ["Datum", "Quittung", "Zahlungsart", "Total", "davon Leistungen", "davon Produkte", "MWST", "davon Rundung bar",
                   "Korrektur nach Abschluss", "Netto nach Korrektur"],
         [[_de(s["day"]), s["number"], s["method"], s["total_chf"], s["services"], s["products"], s["vat"], s.get("rounding") or 0.0,
           _corr_text(s.get("corrections") or []), _net(s)] for s in data["sales"]]
         + ([["", "Total", "", data["sums"]["sales"], "", "", "", "", f"Stornos nach Abschluss im Zeitraum: −{data['sums']['sales_storno']:.2f}",
              data["sums"]["sales_net"]]] if data["sales"] or data.get("cash_corrections") else [])),
        ("Kasse Korrekturen", ["Gebucht", "Quittung", "Tag", "Art", "Zahlungsart", "neu", "Betrag", "Grund"],
         [[_de(c["booked_on"]), c["number"], _de(c["day"]), c["kind"], c["method"], c["to_method"] or "", c["amount_chf"], c["reason"]]
          for c in data.get("cash_corrections", [])]),
        ("Tagesabschlüsse", ["Datum", "Bar erwartet", "Bar gezählt", "Differenz", "Twint", "Karte", "Quittungen"],
         [[_de(c["day"]), c["cash_expected"], c["cash_counted"], c["difference"], c["twint_total"], c["card_total"], c["sales"]] for c in closings]),
        ("Offene Posten (heute)", ["Nummer", "Datum", "Fällig", "Name", "Total", "Offen", "Stand", "Mahnstufe"],
         [[o["number"], _de(_local_day(o["finalized_at"])), _de(o["due_on"]) if o["due_on"] else "", o["name"], o["total"], o["open"],
           o["state"], o["level"]] for o in open_items]),
    ]
    first = True
    for title, header, rows in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = title[:31]
        ws.append(header)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append(safe_row(r))   # «=…» wäre sonst eine Formel (z. B. Name aus einer Bankdatei)
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
