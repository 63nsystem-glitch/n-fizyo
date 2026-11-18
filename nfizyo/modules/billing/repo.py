"""Abrechnung: erledigte Termine → Rechnungsentwurf → abgeschlossen (Nummer, unveränderbar) → ggf. Storno."""
from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta

from ...core.swissids import clean_gln, clean_zsr
from ...core import money
from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import from_iso, today
from ...core import patients as core_patients
from ...knowledge import tariffs as catalog
from ..agenda import api as agenda_api
from . import payment, private, xml500


# --------------------------------------------------------------------------- Taxpunktwerte
# Der KVG-Taxpunktwert der Physiotherapie wird je Kanton mit den Einkaufsgemeinschaften der Versicherer vereinbart
# (tarifsuisse, HSK, CSS) und kann sich je Gruppe unterscheiden. Ohne Wert der Gruppe gilt der Wert der Praxis.
TP_GROUPS = ("tarifsuisse", "hsk", "css", "other")


def insurer_group(db: Database, insurer_id: int | None) -> str:
    ins = core_patients.insurer(db, insurer_id) if insurer_id else None
    group = (ins["tp_group"] if ins is not None and "tp_group" in ins.keys() else None) or ""
    return group if group in TP_GROUPS else ""


def tp_value(db: Database, tariff: str, on: str | None = None, insurer_id: int | None = None) -> float | None:
    t = catalog.TARIFFS.get(tariff)
    if t and t.tp_value is not None:
        return t.tp_value
    day = on or today().isoformat()
    group = insurer_group(db, insurer_id)
    if group:
        value = db.value("SELECT value FROM tax_point_values WHERE practice_id = ? AND tariff = ? AND insurer_group = ?"
                         " AND valid_from <= ? ORDER BY valid_from DESC LIMIT 1", (current_practice_id(), tariff, group, day))
        if value is not None:
            return value
    return db.value("SELECT value FROM tax_point_values WHERE practice_id = ? AND tariff = ? AND insurer_group = '' AND valid_from <= ?"
                    " ORDER BY valid_from DESC LIMIT 1", (current_practice_id(), tariff, day))


def tp_values(db: Database):
    return db.query("SELECT * FROM tax_point_values WHERE practice_id = ? ORDER BY tariff, insurer_group, valid_from DESC",
                    (current_practice_id(),))


def set_tp_value(db: Database, tariff: str, value: float, valid_from: str, note: str | None = None, group: str = "") -> None:
    group = group if group in TP_GROUPS else ""
    db.execute("INSERT INTO tax_point_values (practice_id, tariff, insurer_group, value, valid_from, note, created_at) VALUES (?,?,?,?,?,?,?)"
               " ON CONFLICT(practice_id, tariff, insurer_group, valid_from) DO UPDATE SET value = excluded.value, note = excluded.note",
               (current_practice_id(), tariff, group, value, valid_from, note, utcnow()))
    audit(db, "update", "tax_point_value", None, None, {"tariff": tariff, "value": value, "valid_from": valid_from, "group": group})


# --------------------------------------------------------------------------- Bereit zur Abrechnung
def billed_appointment_ids(db: Database) -> set[int]:
    """Termine, die schon auf einer (nicht stornierten) Rechnung stehen."""
    return {r["appointment_id"] for r in db.query(
        "SELECT l.appointment_id FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id"
        " WHERE l.practice_id = ? AND l.appointment_id IS NOT NULL AND i.status != 'cancelled'", (current_practice_id(),))}


def unbilled_for(db: Database, patient_id: int | None = None):
    """Erledigte, noch nicht verrechnete Termine (aus der Agenda)."""
    billed = billed_appointment_ids(db)
    return [a for a in agenda_api.done_appointments(db, patient_id) if a["id"] not in billed and not a["billed_externally"]]


def ready(db: Database) -> list[dict]:
    """Patient/innen mit erledigten, noch nicht verrechneten Terminen."""
    groups: dict[int, list] = {}
    for a in unbilled_for(db):
        groups.setdefault(a["patient_id"], []).append(a)
    out = []
    people = core_patients.get_many(db, groups.keys())             # eine Abfrage statt zwei je Person
    for pid, appts in groups.items():
        p = people.get(pid)
        if p is None:
            continue
        out.append({"patient_id": pid, "first_name": p["first_name"], "last_name": p["last_name"], "law": p["law"],
                    "insurer_name": p["insurer_name"], "n": len(appts), "first": appts[0]["starts_at"],
                    "last": appts[-1]["starts_at"]})
    return sorted(out, key=lambda r: r["first"])


# --------------------------------------------------------------------------- Rechnungen
def _select() -> str:
    return ("SELECT i.*, p.first_name, p.last_name, p.birthdate, p.sex, p.address AS patient_address, p.zip, p.city,"
            " p.insured_number, p.number AS patient_number, ins.name AS insurer_name, ins.gln AS insurer_gln, u.name AS creator_name"
            " FROM invoices i JOIN patients p ON p.id = i.patient_id LEFT JOIN insurers ins ON ins.id = i.insurer_id"
            " JOIN users u ON u.id = i.created_by")


def get(db: Database, iid: int):
    return db.one(_select() + " WHERE i.id = ? AND i.practice_id = ?", (iid, current_practice_id()))


def listing(db: Database, status: str | None = None, patient_id: int | None = None, limit: int = 200):
    sql = _select() + " WHERE i.practice_id = ?"
    params: list = [current_practice_id()]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if patient_id:
        sql += " AND i.patient_id = ?"
        params.append(patient_id)
    return db.query(sql + " ORDER BY i.id DESC LIMIT ?", (*params, limit))


def lines(db: Database, iid: int):
    return db.query("SELECT l.*, u.name AS therapist_name, u.gln AS therapist_gln FROM invoice_lines l"
                    " LEFT JOIN users u ON u.id = l.therapist_id WHERE l.practice_id = ? AND l.invoice_id = ?"
                    " ORDER BY l.service_date, l.id", (current_practice_id(), iid))


def _minutes(a) -> int:
    start, end = from_iso(a["starts_at"]), from_iso(a["ends_at"])
    if start and end:
        return max(5, int((end - start).total_seconds() // 60))
    return a["type_minutes"] or 30


def ext_factor(tariff: str, day: str) -> float:
    t = catalog.TARIFFS.get(tariff)
    return t.external_factor(day) if t else 1.0


def price(quantity: float, tp: float, value: float | None, factor: float) -> float:
    """Betrag = Anzahl × Taxpunkte × Taxpunktwert × externer Faktor, kaufmännisch auf Rappen gerundet (core/money.py)."""
    return money.times(quantity, tp, value, factor) if value else 0.0


def amounts(db: Database, tariff: str, pos, quantity: float, day: str, chf: float | None = None, insurer_id: int | None = None) -> dict:
    """tp, tp_value, external_factor, amount_chf einer Zeile. Frankenpositionen (z. B. 7362, Material 7361):
    Betrag direkt, ohne Taxpunktwert und externen Faktor (in der XML-Rechnung unit = Betrag, unit_factor = 1)."""
    if pos.fixed_chf:
        unit = money.rp(chf if chf is not None else pos.tp)
        return {"tp": unit, "tp_value": 1.0, "external_factor": 1.0, "amount_chf": money.times(quantity, unit)}
    value = tp_value(db, tariff, day, insurer_id)
    factor = ext_factor(tariff, day)
    return {"tp": pos.tp, "tp_value": value, "external_factor": factor, "amount_chf": price(quantity, pos.tp, value, factor)}


def _video(db: Database, appointment_id: int) -> bool:
    """Fand der Termin als Video-Sitzung statt (jemand ist dem Raum beigetreten)?"""
    from ..video import api as video_api

    return video_api.took_place(db, appointment_id)


def time_units(db: Database, a, pos) -> float:
    """5-Minuten-Einheiten eines Termins: angefangene 5 Minuten zählen, höchstens die Limitation der Position; Gruppe nach der
    Divisormethode auf die Anwesenden verteilt (auf Hundertstel)."""
    units = math.ceil(_minutes(a) / 5)
    if pos.max_minutes:
        units = min(units, pos.max_minutes // 5)
    if pos.divided:
        return round(units / agenda_api.group_size(db, a), 2)
    return float(units)


def line_for(db: Database, a, tariff: str, lang: str, insurer_id: int | None = None) -> dict:
    """Vorschlag für eine Rechnungszeile aus einem erledigten Termin."""
    day = from_iso(a["starts_at"]).date().isoformat()
    t = catalog.TARIFFS.get(tariff)
    video = _video(db, a["id"])
    if t is not None and t.default_code:                       # Zeittarif (pro 5 Minuten)
        pos = catalog.BY_CODE.get((tariff, t.code_for(a["tariff_code"], video) or ""))
        qty = time_units(db, a, pos) if pos is not None and pos.unit == "5min" else 1.0
    else:                                                       # Sitzungspauschale nach Behandlungsart
        pos = catalog.BY_CODE.get((tariff, a["tariff_code"] or ""))
        qty = 1.0
    base = {"appointment_id": a["id"], "service_date": day, "therapist_id": a["therapist_id"], "video": video}
    if pos is None:
        name = (a["type_fr"] if lang == "fr" else a["type_de"]) or ""
        return {**base, "code": None, "label": name, "quantity": qty, "tp": None, "tp_value": tp_value(db, tariff, day, insurer_id),
                "external_factor": ext_factor(tariff, day), "amount_chf": 0.0}
    return {**base, "code": pos.code, "label": catalog.label(pos, lang), "quantity": qty, **amounts(db, tariff, pos, qty, day, insurer_id=insurer_id)}


def tariff_key(law: str | None, *, private_: bool = False, on: str | None = None) -> str | None:
    """Tarif einer Rechnung: amtlicher Tarif nach Gesetz (am Behandlungstag) – oder Preisliste (Selbstzahler, VVG, privat)."""
    if private_ or law in private.PRIVATE_LAWS:
        return private.PRIVATE
    t = catalog.tariff_for_law(law, on)
    return t.key if t else None


FIRST_TREATMENT = "7350"


def imported_sessions(db: Database, patient_id: int, a) -> int:
    """Sitzungen aus der bisherigen Software (prescriptions.sessions_before, Datenübernahme), die vor diesem Termin zählen:
    die der Verordnung des Termins – und, falls der Termin (noch) keiner Verordnung zugeordnet ist oder diese keine hat,
    die einer Verordnung der Patientin / des Patienten aus den 6 Monaten davor. Eigene Abfrage der Abrechnung (Kern-Tabelle)."""
    start = from_iso(a["starts_at"])
    day = start.date().isoformat()
    since = (start - timedelta(days=183)).date().isoformat()
    own = 0
    if a["prescription_id"]:
        own = db.value("SELECT sessions_before FROM prescriptions WHERE id = ? AND practice_id = ? AND patient_id = ?",
                       (a["prescription_id"], current_practice_id(), patient_id), 0) or 0
    if own:
        return int(own)
    return int(db.value(
        "SELECT COALESCE(MAX(sessions_before), 0) FROM prescriptions WHERE practice_id = ? AND patient_id = ? AND sessions_before > 0"
        " AND (issued_on BETWEEN ? AND ? OR (issued_on IS NULL AND substr(created_at, 1, 10) <= ?))",
        (current_practice_id(), patient_id, since, day, day), 0) or 0)


def _first_treatment_due(db: Database, patient_id: int, a) -> bool:
    """Zuschlag erste Behandlung (KVG 7350) vorschlagen? Ja, wenn die Patientin / der Patient in den 6 Monaten davor keine
    erledigte Sitzung hatte – weder in N-Fizyo noch laut Datenübernahme («Bisher n Sitzungen» der Verordnung, F17).
    (Rezidiv mit verändertem Krankheitsbild: von Hand hinzufügen.)"""
    if imported_sessions(db, patient_id, a):
        return False
    start = from_iso(a["starts_at"])
    since = (start - timedelta(days=183)).isoformat()
    earlier = [x for x in agenda_api.done_appointments(db, patient_id, since) if x["id"] != a["id"] and x["starts_at"] < a["starts_at"]]
    return not earlier


def is_private(inv) -> bool:
    return inv["tariff"] == private.PRIVATE


def private_line(db: Database, item, *, service_date: str, quantity: float, lang: str, appointment_id: int | None = None,
                 therapist_id: int | None = None) -> dict:
    if item is None:
        return {"appointment_id": appointment_id, "service_date": service_date, "therapist_id": therapist_id, "code": None,
                "label": "", "quantity": quantity, "unit_price": None, "vat_rate": 0.0, "amount_chf": 0.0}
    return {"appointment_id": appointment_id, "service_date": service_date, "therapist_id": therapist_id, "code": item["code"],
            "label": private.label(item, lang), "quantity": quantity, "unit_price": item["price_chf"],
            "vat_rate": private.rate_for(db, item["vat_rate"]), "price_item_id": item["id"],
            "amount_chf": money.times(quantity, item["price_chf"])}


def private_line_for(db: Database, a, lang: str) -> dict:
    """Selbstzahler: Preis aus der Preisliste (Behandlungsart des Termins, sonst Standardleistung)."""
    day = from_iso(a["starts_at"]).date().isoformat()
    item = private.item_for_type(db, a["treatment_type_id"])
    ln = private_line(db, item, service_date=day, quantity=1.0, lang=lang, appointment_id=a["id"], therapist_id=a["therapist_id"])
    if item is None:
        ln["label"] = (a["type_fr"] if lang == "fr" else a["type_de"]) or ""
    return ln


def create_from_appointments(db: Database, patient, lang: str, *, private_: bool = False) -> int | None:
    """Rechnung(en) aus erledigten Terminen. Wechselt der amtliche Tarif (z. B. neue KVG-Struktur), entsteht pro Tarif
    eine eigene Rechnung. Gibt die erste Rechnungs-Nr. zurück."""
    lang = document_lang(db, patient)
    appts = unbilled_for(db, patient["id"])
    groups: dict[str, list] = {}
    for a in appts:
        key = tariff_key(patient["law"], private_=private_, on=from_iso(a["starts_at"]).date().isoformat())
        if key is not None:
            groups.setdefault(key, []).append(a)
    first = None
    for key, part in groups.items():
        iid = _create_invoice(db, patient, lang, key, part)
        first = first or iid
    return first


def _create_invoice(db: Database, patient, lang: str, key: str, appts: list) -> int | None:
    if not appts:
        return None
    ins = core_patients.insurer(db, patient["insurer_id"])
    tiers = "garant" if key == private.PRIVATE else (ins["tiers"] if ins else "garant")
    law = patient["law"] if key != private.PRIVATE or patient["law"] in private.PRIVATE_LAWS else "privat"
    insurer_id = patient["insurer_id"] if law != "privat" else None
    with db.tx():
        iid = db.execute(
            "INSERT INTO invoices (practice_id, patient_id, insurer_id, prescription_id, law, tariff, tiers, case_number, accident_date,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (current_practice_id(), patient["id"], insurer_id, appts[-1]["prescription_id"],
             law, key, tiers, patient["case_number"] if key != private.PRIVATE else None,
             patient["accident_date"] if key != private.PRIVATE else None, current_user_id(), utcnow()))
        for i, a in enumerate(appts):
            ln = private_line_for(db, a, lang) if key == private.PRIVATE else line_for(db, a, key, lang, insurer_id)
            _insert_line(db, iid, ln)
            marker = catalog.TARIFFS[key].marker_code if key in catalog.TARIFFS else None
            if marker and (key, marker) in catalog.BY_CODE and ln.get("code"):      # Kennzeichnung je Behandlung (ohne Taxpunkte)
                pos = catalog.BY_CODE[(key, marker)]
                _insert_line(db, iid, {"service_date": ln["service_date"], "therapist_id": a["therapist_id"], "code": pos.code,
                                       "label": catalog.label(pos, lang), "quantity": 1.0,
                                       **amounts(db, key, pos, 1.0, ln["service_date"], insurer_id=insurer_id)})
            if i == 0 and FIRST_TREATMENT in catalog.KVG_ALLOWED and ln.get("code") in catalog.KVG_ALLOWED[FIRST_TREATMENT] \
                    and (key, FIRST_TREATMENT) in catalog.BY_CODE and _first_treatment_due(db, patient["id"], a):
                pos = catalog.BY_CODE[(key, FIRST_TREATMENT)]
                _insert_line(db, iid, {"service_date": ln["service_date"], "therapist_id": a["therapist_id"], "code": pos.code,
                                       "label": catalog.label(pos, lang), "quantity": 1.0, **amounts(db, key, pos, 1.0, ln["service_date"], insurer_id=insurer_id)})
        recalc(db, iid)
        audit(db, "create", "invoice", iid, patient["id"], {"lines": len(appts), "tariff": key})
    return iid


def create_from_items(db: Database, patient, entries: list[tuple], lang: str, *, service_date: str | None = None) -> int | None:
    """Rechnung (Entwurf) für Produkte/Abos/Leistungen aus der Preisliste – z. B. Verkauf an der Kasse «auf Rechnung».
    entries = [(price_item, Anzahl)]"""
    entries = [(it, q) for it, q in entries if it is not None and q > 0]
    if not entries:
        return None
    lang = document_lang(db, patient)
    day = service_date or today().isoformat()
    law = patient["law"] if patient["law"] in private.PRIVATE_LAWS else "privat"
    with db.tx():
        iid = db.execute(
            "INSERT INTO invoices (practice_id, patient_id, insurer_id, law, tariff, tiers, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (current_practice_id(), patient["id"], patient["insurer_id"] if law == "VVG" else None, law, private.PRIVATE, "garant",
             current_user_id(), utcnow()))
        for it, q in entries:
            _insert_line(db, iid, private_line(db, it, service_date=day, quantity=float(q), lang=lang, therapist_id=current_user_id()))
        recalc(db, iid)
        audit(db, "create", "invoice", iid, patient["id"], {"lines": len(entries), "tariff": private.PRIVATE, "source": "items"})
    return iid


def _insert_line(db: Database, iid: int, ln: dict) -> int:
    return db.execute(
        "INSERT INTO invoice_lines (practice_id, invoice_id, appointment_id, service_date, therapist_id, code, label, quantity, tp,"
        " tp_value, external_factor, unit_price, vat_rate, price_item_id, amount_chf) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), iid, ln.get("appointment_id"), ln["service_date"], ln.get("therapist_id"), ln.get("code"), ln["label"],
         ln["quantity"], ln.get("tp"), ln.get("tp_value"), ln.get("external_factor", 1.0), ln.get("unit_price"), ln.get("vat_rate", 0.0),
         ln.get("price_item_id"), ln["amount_chf"]))


def recalc(db: Database, iid: int) -> None:
    row = db.one("SELECT COALESCE(SUM(amount_chf), 0) AS total, MIN(service_date) AS f, MAX(service_date) AS t FROM invoice_lines"
                 " WHERE practice_id = ? AND invoice_id = ?", (current_practice_id(), iid))
    vat = private.vat_total(db.query("SELECT vat_rate, amount_chf FROM invoice_lines WHERE practice_id = ? AND invoice_id = ?",
                                     (current_practice_id(), iid)))
    db.execute("UPDATE invoices SET total_chf = ?, vat_chf = ?, period_from = ?, period_to = ? WHERE id = ? AND practice_id = ?"
               " AND status = 'draft'", (money.rp(row["total"]), vat, row["f"], row["t"], iid, current_practice_id()))


def refresh_draft(db: Database, inv):
    """Entwurf: Stammdaten aus der Patientenakte nachführen (Versicherer, Tiers, Schadennummer, Unfalldatum) – z. B. nachdem
    der Empfang die fehlende UVG-Schadennummer im Stammblatt eingetragen hat (F22). Ein anderer Versicherer kann einen anderen
    Taxpunktwert haben: die Zeilen werden dann neu berechnet. Abgeschlossene Rechnungen bleiben unverändert; hat das Gesetz
    gewechselt, bleibt der Entwurf, wie er ist (anderer Tarif: Entwurf löschen und neu erstellen). Gibt die Rechnung zurück."""
    if inv["status"] != "draft" or is_private(inv):
        return inv
    p = core_patients.get(db, inv["patient_id"])
    if p is None or p["law"] != inv["law"]:
        return inv
    changes: dict = {}
    for col in ("case_number", "accident_date"):
        if (p[col] or None) != (inv[col] or None):
            changes[col] = p[col] or None
    if p["insurer_id"] and p["insurer_id"] != inv["insurer_id"]:
        ins = core_patients.insurer(db, p["insurer_id"])
        changes["insurer_id"] = p["insurer_id"]
        if ins is not None and ins["tiers"]:
            changes["tiers"] = ins["tiers"]
    if not changes:
        return inv
    with db.tx():
        db.execute(f"UPDATE invoices SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ? AND practice_id = ? AND status = 'draft'",
                   (*changes.values(), inv["id"], current_practice_id()))
        if "insurer_id" in changes:
            _reprice(db, get(db, inv["id"]))
        audit(db, "refresh", "invoice", inv["id"], inv["patient_id"], {"fields": sorted(changes)})
    return get(db, inv["id"])


def update_line(db: Database, inv, line_id: int, *, code: str | None, quantity: float, lang: str) -> None:
    lang = document_lang(db, _patient_of(db, inv))
    ln = db.one("SELECT * FROM invoice_lines WHERE id = ? AND invoice_id = ? AND practice_id = ?", (line_id, inv["id"], current_practice_id()))
    if is_private(inv):
        it = private.item_by_code(db, code)
        if ln is None or it is None or not (0 < quantity <= 1000):
            return
        new = private_line(db, it, service_date=ln["service_date"], quantity=quantity, lang=lang)
        db.execute("UPDATE invoice_lines SET code = ?, label = ?, quantity = ?, unit_price = ?, vat_rate = ?, price_item_id = ?,"
                   " amount_chf = ? WHERE id = ?", (new["code"], new["label"], quantity, new["unit_price"], new["vat_rate"],
                                                    new["price_item_id"], new["amount_chf"], ln["id"]))
        recalc(db, inv["id"])
        return
    pos = catalog.BY_CODE.get((inv["tariff"], code or ""))
    if ln is None or pos is None or quantity <= 0:
        return
    keep = ln["tp"] if pos.unit == "chf_var" and ln["code"] == pos.code else None
    m = amounts(db, inv["tariff"], pos, quantity, ln["service_date"], keep, inv["insurer_id"])
    label = ln["label"] if pos.unit == "chf_var" and ln["code"] == pos.code else catalog.label(pos, lang)
    db.execute("UPDATE invoice_lines SET code = ?, label = ?, quantity = ?, tp = ?, tp_value = ?, external_factor = ?, amount_chf = ?"
               " WHERE id = ?", (pos.code, label, quantity, m["tp"], m["tp_value"], m["external_factor"], m["amount_chf"], ln["id"]))
    recalc(db, inv["id"])


def add_line(db: Database, inv, *, code: str, quantity: float, service_date: str, lang: str, chf: float | None = None,
             detail: str | None = None) -> None:
    """chf/detail: für Frankenpositionen mit variablem Betrag (Material 7361: Einkaufspreis, Kategorie und Menge)."""
    lang = document_lang(db, _patient_of(db, inv))
    if is_private(inv):
        it = private.item_by_code(db, code)
        if it is None or it["kind"] == "abo" or not (0 < quantity <= 1000):
            return
        _insert_line(db, inv["id"], private_line(db, it, service_date=service_date, quantity=quantity, lang=lang,
                                                 therapist_id=current_user_id()))
        recalc(db, inv["id"])
        return
    pos = catalog.BY_CODE.get((inv["tariff"], code))
    if pos is None:
        return
    if pos.unit == "chf_var" and not (chf and 0 < chf <= 10000):
        return
    label = catalog.label(pos, lang)
    if detail:
        label = f"{label}: {' '.join(detail.split())[:120]}"
    _insert_line(db, inv["id"], {"service_date": service_date, "therapist_id": current_user_id(), "code": pos.code,
                                 "label": label, "quantity": quantity, **amounts(db, inv["tariff"], pos, quantity, service_date, chf, inv["insurer_id"])})
    recalc(db, inv["id"])


def remove_line(db: Database, inv, line_id: int) -> None:
    db.execute("DELETE FROM invoice_lines WHERE id = ? AND invoice_id = ? AND practice_id = ?", (line_id, inv["id"], current_practice_id()))
    recalc(db, inv["id"])


def problems(db: Database, inv, lns) -> list[str]:
    """Was vor dem Abschliessen fehlt (Schlüssel für Übersetzungen)."""
    out = []
    if not lns:
        out.append("no_lines")
    if is_private(inv):
        if any(not ln["code"] or ln["unit_price"] is None for ln in lns):
            out.append("missing_price")
        return out + payment_problems(db)
    if any(not ln["code"] for ln in lns):
        out.append("missing_code")
    if any(ln["code"] and not ln["tp_value"] for ln in lns):
        out.append("missing_tp_value")
    if not inv["insurer_id"]:
        out.append("missing_insurer")
    if inv["law"] == "UVG" and not inv["case_number"]:
        out.append("missing_case_number")
    out += payment_problems(db)
    if inv["prescription_id"]:
        billed = db.value(
            "SELECT COUNT(DISTINCT l.appointment_id) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id"
            " WHERE i.practice_id = ? AND i.prescription_id = ? AND i.status != 'cancelled' AND l.appointment_id IS NOT NULL",
            (current_practice_id(), inv["prescription_id"]), 0)
        billed += db.value("SELECT sessions_before FROM prescriptions WHERE id = ? AND practice_id = ?",   # bisherige Software
                           (inv["prescription_id"], current_practice_id()), 0) or 0
        if billed > catalog.MAX_SESSIONS_PER_PRESCRIPTION:
            out.append("over_nine")
    if inv["tariff"] == "kvg312":
        out += [c for c, blocking in kvg_checks(db, inv, lns) if blocking]
    return out


def _next_number(db: Database) -> str:
    year = today().year
    n = db.value("SELECT COUNT(*) FROM invoices WHERE practice_id = ? AND number LIKE ?", (current_practice_id(), f"{year}-%"), 0)
    return f"{year}-{n + 1:04d}"


def finalize(db: Database, inv) -> str | None:
    if inv["status"] != "draft":
        return None
    st = payment.settings(db)
    iban = payment.clean_iban(st["iban"])
    due = (today() + timedelta(days=int(st["payment_days"]))).isoformat()
    with db.tx():
        number = _next_number(db)
        # nur wenn noch Entwurf (Doppelklick, zwei Fenster): sonst keine zweite Nummer und kein zweiter Eintrag im Protokoll
        if not db.changes("UPDATE invoices SET status = 'final', number = ?, finalized_at = ?, iban = ?, qr_reference = ?, due_on = ?,"
                          " vat_number = ? WHERE id = ? AND practice_id = ? AND status = 'draft'",
                          (number, utcnow(), iban or None, payment.reference_for(iban, inv["id"]) if iban else None, due,
                           private.vat_number(db) if inv["vat_chf"] else None, inv["id"], current_practice_id())):
            return None
        audit(db, "finalize", "invoice", inv["id"], inv["patient_id"], {"number": number, "total": inv["total_chf"]})
    return number


def cancel(db: Database, inv, reason: str) -> None:
    if inv["status"] != "final":
        return
    if db.changes("UPDATE invoices SET status = 'cancelled', cancelled_at = ?, cancel_reason = ? WHERE id = ? AND practice_id = ?"
                  " AND status = 'final'", (utcnow(), reason, inv["id"], current_practice_id())):
        audit(db, "cancel", "invoice", inv["id"], inv["patient_id"], {"reason": reason})


def delete_draft(db: Database, inv) -> None:
    if inv["status"] != "draft":
        return
    with db.tx():
        db.execute("DELETE FROM invoice_lines WHERE invoice_id = ? AND practice_id = ?", (inv["id"], current_practice_id()))
        db.execute("DELETE FROM invoices WHERE id = ? AND practice_id = ? AND status = 'draft'", (inv["id"], current_practice_id()))
        audit(db, "delete", "invoice", inv["id"], inv["patient_id"])


def open_total(db: Database) -> dict:
    return db.one("SELECT COUNT(*) AS n, COALESCE(SUM(total_chf), 0) AS chf FROM invoices WHERE practice_id = ? AND status = 'draft'",
                  (current_practice_id(),))


def reprice_private_drafts(db: Database) -> int:
    """Nach Änderung der Preisliste oder der MWST-Nummer: Selbstzahler-Entwürfe neu berechnen."""
    n = 0
    for inv in db.query("SELECT * FROM invoices WHERE practice_id = ? AND tariff = ? AND status = 'draft'",
                        (current_practice_id(), private.PRIVATE)):
        for ln in lines(db, inv["id"]):
            it = private.item(db, ln["price_item_id"])
            if it is None:
                continue
            db.execute("UPDATE invoice_lines SET unit_price = ?, vat_rate = ?, amount_chf = ? WHERE id = ?",
                       (it["price_chf"], private.rate_for(db, it["vat_rate"]), money.times(ln["quantity"], it["price_chf"]), ln["id"]))
        recalc(db, inv["id"])
        n += 1
    return n


def reprice_drafts(db: Database, tariff: str) -> int:
    """Nach Änderung des Taxpunktwerts: Entwürfe neu berechnen (abgeschlossene Rechnungen bleiben unverändert)."""
    n = 0
    for inv in db.query("SELECT * FROM invoices WHERE practice_id = ? AND tariff = ? AND status = 'draft'", (current_practice_id(), tariff)):
        _reprice(db, inv)
        n += 1
    return n


def _reprice(db: Database, inv) -> None:
    """Zeilen eines Entwurfs mit dem gültigen Taxpunktwert (Versicherergruppe) und externen Faktor neu berechnen."""
    tariff = inv["tariff"]
    for ln in lines(db, inv["id"]):
        if not ln["code"] or ln["tp"] is None:
            continue
        pos = catalog.BY_CODE.get((tariff, ln["code"]))
        if pos is not None and pos.fixed_chf:
            continue                                    # Frankenbetrag hängt nicht vom Taxpunktwert ab
        value = tp_value(db, tariff, ln["service_date"], inv["insurer_id"])
        factor = ext_factor(tariff, ln["service_date"])
        db.execute("UPDATE invoice_lines SET tp_value = ?, external_factor = ?, amount_chf = ? WHERE id = ? AND practice_id = ?",
                   (value, factor, price(ln["quantity"], ln["tp"], value, factor), ln["id"], current_practice_id()))
    recalc(db, inv["id"])


# =========================================================================== Stufe 2: Zahlung, XML, Mahnungen

PAID_TOLERANCE = 0.005


def practice_row(db: Database):
    return db.one("SELECT * FROM practices WHERE id = ?", (current_practice_id(),))


def document_lang(db: Database, patient) -> str:
    """Sprache der Rechnung (PDF, Leistungstexte, QR-Mitteilung): Deutsch oder Französisch der Person, sonst die Praxissprache.
    Nie die Sprache der Person am Bildschirm – sonst stünden französische Leistungen auf einer deutschen Rechnung."""
    if patient is not None and patient["lang"] in ("de", "fr"):
        return patient["lang"]
    practice = practice_row(db)
    return practice["default_lang"] if practice is not None and practice["default_lang"] in ("de", "fr") else "de"


def _patient_of(db: Database, inv):
    return db.one("SELECT * FROM patients WHERE practice_id = ? AND id = ?", (current_practice_id(), inv["patient_id"]))


def payment_problems(db: Database) -> list[str]:
    """Was für die QR-Rechnung fehlt (gilt für alle Rechnungen der Praxis)."""
    out = []
    iban = payment.settings(db)["iban"]
    if not iban or not payment.valid_iban(iban):
        out.append("missing_iban")
    pr = practice_row(db)
    if not (pr["zip"] and pr["city"]):
        out.append("missing_practice_address")
    return out


# --------------------------------------------------------------------------- Zahlungen
def paid_total(db: Database, iid: int) -> float:
    return money.rp(db.value("SELECT COALESCE(SUM(amount_chf), 0) FROM invoice_payments WHERE practice_id = ? AND invoice_id = ?",
                          (current_practice_id(), iid), 0.0))


def fees_total(db: Database, iid: int) -> float:
    return money.rp(db.value("SELECT COALESCE(SUM(fee_chf), 0) FROM invoice_reminders WHERE practice_id = ? AND invoice_id = ?",
                          (current_practice_id(), iid), 0.0))


def balance(db: Database, inv) -> dict:
    paid = paid_total(db, inv["id"])
    fees = fees_total(db, inv["id"])
    open_ = money.rp(inv["total_chf"] + fees - paid) if inv["status"] == "final" else 0.0
    if inv["status"] == "draft":
        state = "draft"
    elif inv["status"] == "cancelled":
        state = "cancelled"
    elif open_ <= PAID_TOLERANCE:
        state = "paid"
    elif inv["due_on"] and inv["due_on"] < today().isoformat():
        state = "overdue"
    elif paid > 0:
        state = "partial"
    else:
        state = "open"
    return {"paid": paid, "fees": fees, "open": max(open_, 0.0), "state": state, "overpaid": money.rp(-open_) if open_ < -PAID_TOLERANCE else 0.0}


PAYMENT_METHODS = ("bank", "cash", "twint", "card")
PAYMENT_KINDS = ("payment", "refund", "reversal")


def add_payment(db: Database, *, amount: float, booked_on: str, source: str, invoice_id: int | None = None,
                reference: str | None = None, debtor: str | None = None, bank_ref: str | None = None,
                import_id: int | None = None, note: str | None = None, method: str = "bank", kind: str = "payment",
                reversal_of: int | None = None, rounding: float = 0.0, message: str | None = None) -> int | None:
    """Zahlung erfassen. Gleiche Bankbuchung zweimal → None (kein Doppel). Rückzahlung/Storno: negativer Betrag.
    rounding: nur bar – Rundung auf 5 Rappen (Bargeld = amount + rounding, siehe cash_amounts()). message: Mitteilung der Bank."""
    if bank_ref and db.value("SELECT 1 FROM invoice_payments WHERE practice_id = ? AND bank_ref = ?", (current_practice_id(), bank_ref)):
        return None
    method = method if method in PAYMENT_METHODS else "bank"
    kind = kind if kind in PAYMENT_KINDS else "payment"
    rounding = money.rp(rounding) if method == "cash" and abs(rounding) < money.CASH_TOLERANCE else 0.0
    pid = db.execute(
        "INSERT INTO invoice_payments (practice_id, invoice_id, amount_chf, booked_on, reference, debtor, source, bank_ref, import_id,"
        " note, method, kind, reversal_of, rounding_chf, message, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), invoice_id, money.rp(amount), booked_on, reference, (debtor or "")[:140] or None, source, bank_ref,
         import_id, note, method, kind, reversal_of, rounding, (message or "")[:280] or None, current_user_id(), utcnow()))
    audit(db, "create", "payment", pid, None, {"invoice": invoice_id, "amount": money.rp(amount), "source": source, "method": method,
                                               "kind": kind, **({"reversal_of": reversal_of} if reversal_of else {}),
                                               **({"rounding": rounding} if rounding else {})})
    return pid


def cash_amounts(open_amount: float, amount: float, method: str) -> tuple[float, float]:
    """(gutgeschrieben, Rundung) einer Zahlung: bar auf 5 Rappen (Restbetrag unter 5 Rappen wird als Rundung ausgebucht,
    die Rechnung ist dann bezahlt – keine Mahnung für ein paar Rappen); Bank, Twint, Karte auf den Rappen genau."""
    if method != "cash":
        return money.rp(amount), 0.0
    credited, _cash, rounding = money.cash_settle(open_amount, amount)
    return credited, rounding


def add_manual_payment(db: Database, inv, *, amount: float, booked_on: str, method: str, note: str | None = None) -> int | None:
    """Zahlung von Hand auf der Rechnung (Empfang): bar auf 5 Rappen gerundet, sonst genau."""
    method = method if method in PAYMENT_METHODS else "bank"
    credited, rounding = cash_amounts(balance(db, inv)["open"], amount, method)
    if credited <= 0:
        return None
    return add_payment(db, amount=credited, booked_on=booked_on, source="manual", invoice_id=inv["id"], note=note, method=method,
                       rounding=rounding)


def payment_row(db: Database, payment_id: int):
    return db.one("SELECT pm.*, (SELECT r.id FROM invoice_payments r WHERE r.reversal_of = pm.id) AS reversed_by_id"
                  " FROM invoice_payments pm WHERE pm.id = ? AND pm.practice_id = ?", (payment_id, current_practice_id()))


def _movable(p) -> bool:
    """Zuordnung ändern geht nur bei einem Eingang, der nicht storniert ist."""
    return p is not None and p["kind"] == "payment" and not p["reversed_by_id"]


def _move(db: Database, p, to_invoice_id: int | None, reason: str | None) -> None:
    db.execute("INSERT INTO payment_moves (practice_id, payment_id, from_invoice_id, to_invoice_id, moved_on, reason, created_by, created_at)"
               " VALUES (?,?,?,?,?,?,?,?)", (current_practice_id(), p["id"], p["invoice_id"], to_invoice_id, today().isoformat(),
                                             (reason or "")[:200] or None, current_user_id(), utcnow()))


def assign_payment(db: Database, payment_id: int, invoice_id: int) -> bool:
    inv = get(db, invoice_id)
    p = payment_row(db, payment_id)
    if inv is None or inv["status"] != "final" or not _movable(p) or p["invoice_id"] is not None or p["dismissed_at"]:
        return False
    with db.tx():
        _move(db, p, invoice_id, None)
        db.execute("UPDATE invoice_payments SET invoice_id = ? WHERE id = ? AND practice_id = ? AND invoice_id IS NULL",
                   (invoice_id, payment_id, current_practice_id()))
        audit(db, "assign", "payment", payment_id, inv["patient_id"], {"invoice": invoice_id})
    return True


def unassign_payment(db: Database, payment_id: int, reason: str) -> bool:
    """Falsch zugeordnete Zahlung zurück in «Ohne Zuordnung» (die Rechnung ist wieder offen). Mit Grund, protokolliert."""
    p = payment_row(db, payment_id)
    reason = (reason or "").strip()[:200]
    if not reason or not _movable(p) or p["invoice_id"] is None:
        return False
    inv = get(db, p["invoice_id"])
    with db.tx():
        _move(db, p, None, reason)
        db.execute("UPDATE invoice_payments SET invoice_id = NULL WHERE id = ? AND practice_id = ?", (payment_id, current_practice_id()))
        audit(db, "unassign", "payment", payment_id, inv["patient_id"] if inv else None, {"invoice": p["invoice_id"], "reason": reason})
    return True


def reverse_payment(db: Database, payment_id: int, reason: str, day: str | None = None) -> int | None:
    """Storno einer gebuchten Zahlung (z. B. doppelt erfasst): Gegenbuchung mit negativem Betrag – nichts wird gelöscht."""
    p = payment_row(db, payment_id)
    reason = (reason or "").strip()[:200]
    if p is None or not reason or p["kind"] == "reversal" or p["reversed_by_id"]:
        return None
    with db.tx():
        rid = add_payment(db, amount=-p["amount_chf"], booked_on=day or today().isoformat(), source="manual", invoice_id=p["invoice_id"],
                          reference=p["reference"], debtor=p["debtor"], note=reason, method=p["method"], kind="reversal",
                          reversal_of=p["id"], rounding=-(p["rounding_chf"] or 0.0))
        audit(db, "reverse", "payment", p["id"], None, {"reversal": rid, "amount": p["amount_chf"], "reason": reason})
    return rid


def refund_payment(db: Database, inv, *, amount: float, method: str, day: str, reason: str) -> int | None:
    """Rückzahlung an die Patientin / den Patienten (z. B. zu viel bezahlt): negative Zahlung auf der Rechnung.
    Bar auf 5 Rappen: liegt das Bargeld weniger als 5 Rappen neben dem Guthaben (zu viel bezahlt bzw. bei Storno alles Bezahlte),
    wird genau das Guthaben ausgebucht und die Differenz als Rundung geführt."""
    reason = (reason or "").strip()[:200]
    if inv["status"] == "draft" or not reason or method not in PAYMENT_METHODS or not (0 < amount <= 100000):
        return None
    paid = paid_total(db, inv["id"])
    target = paid if inv["status"] == "cancelled" else balance(db, inv)["overpaid"]
    credited, rounding = cash_amounts(-target if target > 0 else -money.cash(amount), -amount, method)
    if not (0 < -credited <= paid + PAID_TOLERANCE):
        return None
    return add_payment(db, amount=credited, booked_on=day, source="manual", invoice_id=inv["id"], note=reason, method=method,
                       kind="refund", rounding=rounding)


def dismiss_payment(db: Database, payment_id: int, reason: str) -> bool:
    """Bankzeile ohne Zuordnung ausblenden (z. B. keine Patientenzahlung). Bleibt gebucht, mit Grund, rückgängig machbar."""
    p = payment_row(db, payment_id)
    reason = (reason or "").strip()[:200]
    if not reason or not _movable(p) or p["invoice_id"] is not None or p["dismissed_at"]:
        return False
    db.execute("UPDATE invoice_payments SET dismissed_at = ?, dismissed_by = ?, dismiss_reason = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), current_user_id(), reason, payment_id, current_practice_id()))
    audit(db, "dismiss", "payment", payment_id, None, {"reason": reason})
    return True


def undismiss_payment(db: Database, payment_id: int) -> bool:
    p = payment_row(db, payment_id)
    if p is None or not p["dismissed_at"]:
        return False
    db.execute("UPDATE invoice_payments SET dismissed_at = NULL, dismissed_by = NULL, dismiss_reason = NULL WHERE id = ? AND practice_id = ?",
               (payment_id, current_practice_id()))
    audit(db, "undismiss", "payment", payment_id, None, {"reason": p["dismiss_reason"]})
    return True


def payments_for(db: Database, iid: int):
    return db.query("SELECT pm.*, (SELECT r.id FROM invoice_payments r WHERE r.reversal_of = pm.id) AS reversed_by_id"
                    " FROM invoice_payments pm WHERE pm.practice_id = ? AND pm.invoice_id = ? ORDER BY pm.booked_on, pm.id",
                    (current_practice_id(), iid))


INVOICE_NUMBER_RX = re.compile(r"(?<![0-9])(?:V-)?(20[0-9]{2})[-/ ]?([0-9]{4})(?![0-9])")


def suggest_number(db: Database, *texts: str | None) -> str | None:
    """Rechnungsnummer aus der Mitteilung der Bank («Rechnung 2026-0099 Zahlung») – nur wenn es sie als offene Rechnung gibt."""
    for text in texts:
        for m in INVOICE_NUMBER_RX.finditer(text or ""):
            number = f"{m.group(1)}-{m.group(2)}"
            if db.value("SELECT 1 FROM invoices WHERE practice_id = ? AND number = ? AND status = 'final'", (current_practice_id(), number)):
                return number
    return None


def unmatched_payments(db: Database) -> list[dict]:
    """Eingänge ohne Zuordnung, die noch zu prüfen sind (nicht storniert, nicht ausgeblendet). suggestion = Rechnungsnummer aus
    der Mitteilung der zahlenden Person (falls vorhanden)."""
    rows = db.query("SELECT pm.* FROM invoice_payments pm WHERE pm.practice_id = ? AND pm.invoice_id IS NULL AND pm.kind = 'payment'"
                    " AND pm.dismissed_at IS NULL AND NOT EXISTS (SELECT 1 FROM invoice_payments r WHERE r.reversal_of = pm.id)"
                    " ORDER BY pm.booked_on DESC, pm.id DESC", (current_practice_id(),))
    return [dict(r) | {"suggestion": suggest_number(db, r["message"], r["reference"])} for r in rows]


def dismissed_payments(db: Database, limit: int = 50):
    return db.query("SELECT pm.*, u.name AS dismissed_name FROM invoice_payments pm LEFT JOIN users u ON u.id = pm.dismissed_by"
                    " WHERE pm.practice_id = ? AND pm.invoice_id IS NULL AND pm.dismissed_at IS NOT NULL"
                    " ORDER BY pm.dismissed_at DESC LIMIT ?", (current_practice_id(), limit))


def import_payments(db: Database, filename: str, data: bytes) -> dict:
    """camt.054 / camt.053 einlesen: Buchungen über die Referenz der Rechnung zuordnen, Rest zur Prüfung."""
    import hashlib

    from . import camt

    sha = hashlib.sha256(data).hexdigest()
    if db.value("SELECT 1 FROM payment_imports WHERE practice_id = ? AND sha256 = ?", (current_practice_id(), sha)):
        return {"duplicate": True, "entries": 0, "matched": 0, "unmatched": 0, "skipped": 0}
    entries = camt.parse(data)
    imp = db.execute("INSERT INTO payment_imports (practice_id, filename, sha256, entries, created_by, created_at) VALUES (?,?,?,?,?,?)",
                     (current_practice_id(), filename[:200], sha, len(entries), current_user_id(), utcnow()))
    matched = unmatched = skipped = 0
    for e in entries:
        if e.credit is False or e.currency != "CHF":
            skipped += 1
            continue
        iid = payment.invoice_id_from_reference(e.reference)
        inv = get(db, iid) if iid else None
        if inv is not None and (inv["status"] != "final" or (inv["qr_reference"] or "") != (e.reference or "").replace(" ", "").upper()):
            inv = None
        if inv is None:
            # Referenz einer Firmenrechnung? Dann bucht das Modul «Firmenkunden» die Zahlung selbst.
            from ..firms import api as firms_api

            claimed = firms_api.claim_payment(db, reference=e.reference, amount=e.amount, booked_on=e.booked_on, debtor=e.debtor,
                                              bank_ref=e.bank_ref)
            if claimed is not None:
                if claimed:
                    matched += 1
                else:
                    skipped += 1
                continue
        new = add_payment(db, amount=e.amount, booked_on=e.booked_on, source="camt", invoice_id=inv["id"] if inv else None,
                          reference=e.reference, debtor=e.debtor, bank_ref=e.bank_ref, import_id=imp, message=e.message)
        if new is None:
            skipped += 1
        elif inv is not None:
            matched += 1
        else:
            unmatched += 1
    db.execute("UPDATE payment_imports SET matched = ? WHERE id = ?", (matched, imp))
    audit(db, "import", "payments", imp, None, {"entries": len(entries), "matched": matched, "unmatched": unmatched})
    return {"duplicate": False, "entries": len(entries), "matched": matched, "unmatched": unmatched, "skipped": skipped}


# --------------------------------------------------------------------------- Offene Posten und Mahnungen
def reminders_for(db: Database, iid: int):
    return db.query("SELECT r.*, u.name AS creator_name FROM invoice_reminders r LEFT JOIN users u ON u.id = r.created_by"
                    " WHERE r.practice_id = ? AND r.invoice_id = ? ORDER BY r.level", (current_practice_id(), iid))


def next_reminder(db: Database, inv) -> int | None:
    """Nächste Mahnstufe, wenn die Rechnung (bzw. die letzte Mahnung) überfällig ist. Höchstens Stufe 3."""
    if inv["status"] != "final" or balance(db, inv)["open"] <= PAID_TOLERANCE:
        return None
    if db.value("SELECT 1 FROM invoice_responses WHERE practice_id = ? AND invoice_id = ? AND handled_at IS NULL AND kind = 'rejected'",
                (current_practice_id(), inv["id"])):
        return None                    # vom Versicherer zurückgewiesen und noch nicht erledigt: erst klären, nicht mahnen
    rem = reminders_for(db, inv["id"])
    last_due = rem[-1]["due_on"] if rem else inv["due_on"]
    if len(rem) >= 3 or not last_due or last_due >= today().isoformat():
        return None
    return len(rem) + 1


def create_reminder(db: Database, inv, level: int) -> int | None:
    if next_reminder(db, inv) != level:
        return None
    st = payment.settings(db)
    fee = {1: 0.0, 2: float(st["reminder_fee_2"]), 3: float(st["reminder_fee_3"])}[level]
    open_before = balance(db, inv)["open"]
    rid = db.execute(
        "INSERT INTO invoice_reminders (practice_id, invoice_id, level, fee_chf, open_chf, due_on, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (current_practice_id(), inv["id"], level, fee, open_before,
         (today() + timedelta(days=int(st["reminder_days"]))).isoformat(), current_user_id(), utcnow()))
    audit(db, "create", "reminder", rid, inv["patient_id"], {"invoice": inv["id"], "level": level, "fee": fee})
    return rid


def _open_candidates(db: Database):
    """Abgeschlossene Rechnungen, deren Saldo nicht null ist (offen oder überzahlt) – ALLE, ohne Obergrenze.
    Früher: nur die neuesten 2000 Rechnungen → ältere offene fielen nach einem Jahr still aus Mahnwesen und Offenen Posten."""
    pid = current_practice_id()
    return db.query(
        _select() +
        " LEFT JOIN (SELECT invoice_id, SUM(amount_chf) AS s FROM invoice_payments WHERE practice_id = ? GROUP BY invoice_id) pay"
        " ON pay.invoice_id = i.id"
        " LEFT JOIN (SELECT invoice_id, SUM(fee_chf) AS s FROM invoice_reminders WHERE practice_id = ? GROUP BY invoice_id) fee"
        " ON fee.invoice_id = i.id"
        " WHERE i.practice_id = ? AND i.status = 'final'"
        " AND ABS(i.total_chf + COALESCE(fee.s, 0) - COALESCE(pay.s, 0)) > ?"
        " ORDER BY i.id DESC", (pid, pid, pid, PAID_TOLERANCE))


def open_items(db: Database) -> list[dict]:
    out = []
    for inv in _open_candidates(db):
        b = balance(db, inv)
        if b["open"] > PAID_TOLERANCE or b["overpaid"]:
            out.append({"inv": inv, "b": b, "level": len(reminders_for(db, inv["id"])), "next": next_reminder(db, inv)})
    return sorted(out, key=lambda x: (x["inv"]["due_on"] or "", x["inv"]["id"]))


# --------------------------------------------------------------------------- Elektronische Rechnung
def _party_practice(pr) -> xml500.Party:
    return xml500.Party(name=pr["name"], street=pr["street"], house_no=pr["house_no"], zip=pr["zip"], city=pr["city"],
                        gln=clean_gln(pr["gln"]), zsr=clean_zsr(pr["zsr"]), phone=pr["phone"], email=pr["email"])


def xml_data(db: Database, inv) -> xml500.Data:
    pr = practice_row(db)
    lns = lines(db, inv["id"])
    patient = core_patients.get(db, inv["patient_id"])
    ins = core_patients.insurer(db, inv["insurer_id"]) if inv["insurer_id"] else None
    therapist_ids = [ln["therapist_id"] for ln in lns if ln["therapist_id"]]
    main_id = max(set(therapist_ids), key=therapist_ids.count) if therapist_ids else None
    users = {u["id"]: u for u in db.query("SELECT id, name, gln, zsr FROM users WHERE practice_id = ?", (current_practice_id(),))}
    main = users.get(main_id)

    def person(u) -> xml500.Party:
        parts = (u["name"] if u else "").split()
        return xml500.Party(name=parts[-1] if parts else "", first_name=" ".join(parts[:-1]) or (parts[-1] if parts else "-"),
                            street=pr["street"], house_no=pr["house_no"], zip=pr["zip"], city=pr["city"],
                            gln=clean_gln(u["gln"]) if u else None, zsr=clean_zsr(u["zsr"]) if u else None)

    referrer = None
    if patient["referrer_id"]:
        ref = db.one("SELECT * FROM referrers WHERE id = ? AND practice_id = ?", (patient["referrer_id"], current_practice_id()))
        if ref is not None and clean_gln(ref["gln"]):   # ungültige GLN der Zuweisenden: weglassen statt ungültiges XML
            parts = ref["name"].replace("Dr. med.", "").replace("Dr", "").split()
            referrer = xml500.Party(name=parts[-1] if parts else ref["name"], first_name=" ".join(parts[:-1]) or "-",
                                    zip=pr["zip"], city=pr["city"], gln=clean_gln(ref["gln"]), zsr=clean_zsr(ref["zsr"]))
    diagnosis = None
    if inv["prescription_id"]:
        diagnosis = db.value("SELECT diagnosis FROM prescriptions WHERE id = ? AND practice_id = ?",
                             (inv["prescription_id"], current_practice_id()))
    reason = "accident" if inv["law"] == "UVG" or (inv["law"] == "KVG" and inv["accident_date"]) else "disease"
    tcode = xml500.tariff_code(inv["tariff"])
    xlines = [xml500.Line(record=i + 1, tariff_code=tcode, code=ln["code"] or "", name=ln["label"], quantity=ln["quantity"],
                          date=ln["service_date"], provider_gln=(users.get(ln["therapist_id"]) or {"gln": None})["gln"] or "",
                          unit=ln["tp"] or 0.0, unit_factor=ln["tp_value"] or 0.0, external_factor=ln["external_factor"] or 1.0,
                          amount=ln["amount_chf"]) for i, ln in enumerate(lns)]
    p_street, p_house = core_patients.split_street(patient["address"])
    insurer = None
    if ins is not None:
        insurer = xml500.Party(name=ins["name"], street=ins["street"], zip=ins["zip"], city=ins["city"], gln=ins["gln"])
    return xml500.Data(
        number=inv["number"] or f"E{inv['id']}", finalized_at=inv["finalized_at"] or utcnow(), lang=pr["default_lang"],
        tiers=inv["tiers"], law=inv["law"], biller=_party_practice(pr), provider=person(main), insurer=insurer,
        patient=xml500.Party(name=patient["last_name"], first_name=patient["first_name"], street=p_street, house_no=p_house,
                             zip=patient["zip"], city=patient["city"], phone=patient["phone"]),
        patient_sex=patient["sex"], patient_birthdate=patient["birthdate"], patient_ssn=patient["ahv_number"],
        insured_id=patient["insured_number"], case_id=inv["case_number"], case_date=inv["accident_date"], canton=(pr["canton"] or "").strip().upper() or None,
        reason=reason, period_from=inv["period_from"] or today().isoformat(), period_to=inv["period_to"] or today().isoformat(),
        diagnosis=diagnosis, lines=xlines, total=inv["total_chf"], iban=inv["iban"] or payment.clean_iban(payment.settings(db)["iban"]),
        reference=inv["qr_reference"] or payment.reference_for(payment.clean_iban(payment.settings(db)["iban"]), inv["id"]),
        referrer=referrer, paid=paid_total(db, inv["id"]))


def add_transmission(db: Database, inv, *, kind: str, guid: str, modus: str, channel: str, status: str, xml: bytes,
                     reminder_level: int | None = None, reference: str | None = None, error: str | None = None,
                     document_reference: str | None = None, correlation_reference: str | None = None,
                     error_code: str | None = None, resolution: str | None = None) -> int:
    tid = db.execute(
        "INSERT INTO invoice_transmissions (practice_id, invoice_id, kind, reminder_level, guid, modus, channel, status, reference, error,"
        " xml, created_by, created_at, sent_at, document_reference, correlation_reference, error_code, resolution)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), inv["id"], kind, reminder_level, guid, modus, channel, status, reference, (error or "")[:500] or None,
         xml.decode("utf-8"), current_user_id(), utcnow(), utcnow() if status == "sent" else None, document_reference,
         correlation_reference, error_code, resolution))
    audit(db, "transmit" if channel == "medidata" else "export", "invoice", inv["id"], inv["patient_id"],
          {"kind": kind, "channel": channel, "status": status, "modus": modus})
    return tid


def transmissions_for(db: Database, iid: int):
    rows = db.query("SELECT id, kind, reminder_level, guid, modus, channel, status, reference, error, error_code, created_at, sent_at,"
                    " checked_at, md_status, md_error_reason, md_detail, md_checked_at, resolution"
                    " FROM invoice_transmissions WHERE practice_id = ? AND invoice_id = ? ORDER BY id DESC", (current_practice_id(), iid))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["md_detail"] = json.loads(r["md_detail"]) if r["md_detail"] else []
        except ValueError:
            d["md_detail"] = []
        out.append(d)
    return out


def transmission(db: Database, tid: int):
    return db.one("SELECT * FROM invoice_transmissions WHERE id = ? AND practice_id = ?", (tid, current_practice_id()))


def mark_transmission(db: Database, tid: int, status: str) -> bool:
    """Unklare MediData-Sendung nach Prüfung im Portal: «sent» (ist angekommen) oder «failed» (nicht angekommen → neu senden)."""
    t = transmission(db, tid)
    if t is None or t["status"] != "unknown" or status not in ("sent", "failed"):
        return False
    db.execute("UPDATE invoice_transmissions SET status = ?, checked_by = ?, checked_at = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END"
               " WHERE id = ? AND practice_id = ?", (status, current_user_id(), utcnow(), status, utcnow(), tid, current_practice_id()))
    audit(db, "mark", "invoice_transmission", tid, None, {"invoice": t["invoice_id"], "status": status})
    return True



# --------------------------------------------------------------------------- KVG 312: Regeln der Tarifstruktur
def kvg_checks(db: Database, inv, lns) -> list[tuple[str, bool]]:
    """(Schlüssel, blockiert?) – Kombinationsregeln und Limitationen der Tarifstruktur 312.
    Blockierend nur, was die Tarifstruktur eindeutig ausschliesst; der Rest ist ein Hinweis zum Prüfen."""
    out: list[tuple[str, bool]] = []
    by_day: dict[str, list] = {}
    for ln in lns:
        if ln["code"]:
            by_day.setdefault(ln["service_date"], []).append(ln["code"])
    for day, codes in by_day.items():
        sessions = [c for c in codes if c in {"7301", "7311", "7313", "7330", "7340"}]
        if len(sessions) > 1:
            out.append(("kvg_two_sessions", False))     # nur bei zwei ärztlich verordneten Behandlungen am Tag erlaubt
        for c in codes:
            allowed = catalog.KVG_ALLOWED.get(c)
            if allowed is not None and not (set(sessions) & allowed):
                out.append(("kvg_combination", True))
    patient = core_patients.get(db, inv["patient_id"])
    if "7351" in {ln["code"] for ln in lns} and patient and patient["birthdate"]:
        first = min(ln["service_date"] for ln in lns if ln["code"] == "7351")
        bd = date.fromisoformat(patient["birthdate"])
        if date.fromisoformat(first) >= date(bd.year + 6, bd.month, min(bd.day, 28)):
            out.append(("kvg_7351_age", True))
    probes = [ln for ln in lns if ln["code"] in ("7362", "7363")]
    if probes:
        years = {ln["service_date"][:4] for ln in probes}
        earlier = db.value(
            "SELECT COUNT(*) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE l.practice_id = ? AND i.patient_id = ?"
            " AND i.id != ? AND i.status != 'cancelled' AND l.code IN ('7362', '7363') AND substr(l.service_date, 1, 4) IN ({})"
            .format(",".join("?" for _ in years)), (current_practice_id(), inv["patient_id"], inv["id"], *sorted(years)), 0)
        if len(probes) > 1 or earlier:
            out.append(("kvg_probe_year", True))
    if "7350" in {ln["code"] for ln in lns}:
        earlier = db.value(
            "SELECT MAX(l.service_date) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE l.practice_id = ?"
            " AND i.patient_id = ? AND i.id != ? AND i.status != 'cancelled' AND l.code = '7350'",
            (current_practice_id(), inv["patient_id"], inv["id"]))
        if earlier:
            first = min(ln["service_date"] for ln in lns if ln["code"] == "7350")
            between = db.value(
                "SELECT COUNT(*) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE l.practice_id = ? AND i.patient_id = ?"
                " AND i.status != 'cancelled' AND l.code IN ('7301', '7311', '7313', '7330', '7340') AND l.service_date > ?"
                " AND l.service_date < ?", (current_practice_id(), inv["patient_id"], earlier, first), 0)
            if between < 36 and (date.fromisoformat(first) - date.fromisoformat(earlier)).days <= 183:
                out.append(("kvg_7350_repeat", False))  # nur bei Rezidiv mit verändertem Krankheitsbild
    seen: list[tuple[str, bool]] = []
    for c in out:
        if c not in seen:
            seen.append(c)
    return seen


def warnings(db: Database, inv, lns) -> list[str]:
    """Hinweise, die das Abschliessen nicht verhindern (z. B. zwei Sitzungen am selben Tag)."""
    out = [c for c, blocking in kvg_checks(db, inv, lns) if not blocking] if inv["tariff"] == "kvg312" else []
    if inv["tariff"] == "kvg312" and any(ln["appointment_id"] and _video(db, ln["appointment_id"]) for ln in lns):
        out.append("kvg_video")
    for ln in lns:
        pos = catalog.BY_CODE.get((inv["tariff"], ln["code"] or ""))
        if pos is None:
            continue
        if pos.divided:
            out.append("group_divided")              # Anzahl Anwesende aus der Agenda – vor dem Abschliessen prüfen
        if pos.max_minutes and ln["appointment_id"]:
            a = agenda_api.get(db, ln["appointment_id"])
            if a is not None and _minutes(a) > pos.max_minutes:
                out.append("max_minutes")
    return list(dict.fromkeys(out))
