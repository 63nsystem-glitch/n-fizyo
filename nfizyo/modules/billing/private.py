"""Selbstzahler und Zusatzversicherung (VVG, privat): Preisliste der Praxis und Mehrwertsteuer.

Preise sind Endpreise (inkl. MWST). MWST wird nur ausgewiesen, wenn die Praxis eine MWST-Nummer (UID) eingetragen hat.
Ärztlich verordnete Physiotherapie ist von der MWST ausgenommen; Produkte, Fitness/Training ohne Verordnung und
Wellness sind steuerbar (Normalsatz 8,1 % seit 1.1.2024, reduziert 2,6 %). Welche Leistung steuerbar ist,
entscheidet die Praxis (mit Treuhand) – N-Fizyo schlägt nur vor.
"""
from __future__ import annotations

import re

from ...core import money
from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id

PRIVATE = "private"                 # Tarif-Schlüssel der Selbstzahler-Rechnungen
PRIVATE_LAWS = ("VVG", "privat")
KINDS = ("service", "product", "abo")
VAT_RATES = (0.0, 2.6, 8.1)
DEFAULT_VAT = {"service": 0.0, "product": 8.1, "abo": 8.1}

_UID_RX = re.compile(r"^CHE[-\s.]?(\d{3})[.\s]?(\d{3})[.\s]?(\d{3})(?:\s*(?:MWST|TVA|IVA))?$", re.I)
_UID_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4)


def clean_uid(value: str | None) -> str | None:
    """«che 123 456 788 mwst» → «CHE-123.456.788» (mit Prüfziffer, BFS) – sonst None."""
    m = _UID_RX.match((value or "").strip())
    if not m:
        return None
    digits = "".join(m.groups())
    check = 11 - sum(int(d) * w for d, w in zip(digits[:8], _UID_WEIGHTS)) % 11
    check = 0 if check == 11 else check
    if check == 10 or check != int(digits[8]):
        return None
    return f"CHE-{digits[:3]}.{digits[3:6]}.{digits[6:]}"


def label(item, lang: str) -> str:
    return (item["name_fr"] if lang == "fr" else item["name_de"]) or item["name_de"]


def items(db: Database, kinds: tuple[str, ...] | None = None, *, active_only: bool = True):
    sql = "SELECT * FROM price_items WHERE practice_id = ?"
    params: list = [current_practice_id()]
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    if active_only:
        sql += " AND active = 1"
    return db.query(sql + " ORDER BY CASE kind WHEN 'service' THEN 0 WHEN 'abo' THEN 1 ELSE 2 END, code", params)


def item(db: Database, item_id: int | None):
    if not item_id:
        return None
    return db.one("SELECT * FROM price_items WHERE id = ? AND practice_id = ?", (item_id, current_practice_id()))


def item_by_code(db: Database, code: str | None):
    return db.one("SELECT * FROM price_items WHERE practice_id = ? AND code = ? AND active = 1", (current_practice_id(), code or ""))


def item_for_type(db: Database, treatment_type_id: int | None):
    """Preis für einen Termin: Leistung dieser Behandlungsart, sonst die Leistung ohne Behandlungsart."""
    if treatment_type_id:
        row = db.one("SELECT * FROM price_items WHERE practice_id = ? AND kind = 'service' AND active = 1 AND treatment_type_id = ?"
                     " ORDER BY id LIMIT 1", (current_practice_id(), treatment_type_id))
        if row is not None:
            return row
    return db.one("SELECT * FROM price_items WHERE practice_id = ? AND kind = 'service' AND active = 1 AND treatment_type_id IS NULL"
                  " ORDER BY id LIMIT 1", (current_practice_id(),))


def validate(values: dict) -> list[str]:
    """Fehler-Schlüssel (billing.price.err_*)."""
    errs = []
    if values.get("kind") not in KINDS:
        errs.append("kind")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,19}", values.get("code") or ""):
        errs.append("code")
    if not (values.get("name_de") or "").strip():
        errs.append("name")
    price = values.get("price_chf")
    if price is None or not (0 <= price <= 100000):
        errs.append("price")
    if values.get("vat_rate") not in VAT_RATES:
        errs.append("vat")
    if values.get("kind") == "abo" and not values.get("abo_days"):
        errs.append("abo_days")
    return errs


def save_item(db: Database, values: dict, item_id: int | None = None) -> int | None:
    """Neu oder ändern. Gleicher Code zweimal → None."""
    cols = ("kind", "code", "name_de", "name_fr", "price_chf", "vat_rate", "treatment_type_id", "abo_sessions", "abo_days")
    data = {k: values.get(k) for k in cols}
    data["name_fr"] = (data["name_fr"] or "").strip() or data["name_de"]
    if data["kind"] != "abo":
        data["abo_sessions"] = data["abo_days"] = None
    if data["kind"] != "service":
        data["treatment_type_id"] = None
    clash = db.value("SELECT id FROM price_items WHERE practice_id = ? AND code = ? AND id != ?",
                     (current_practice_id(), data["code"], item_id or 0))
    if clash:
        return None
    if item_id:
        if item(db, item_id) is None:
            return None
        db.execute(f"UPDATE price_items SET {', '.join(f'{k} = ?' for k in cols)}, updated_at = ? WHERE id = ? AND practice_id = ?",
                   (*[data[k] for k in cols], utcnow(), item_id, current_practice_id()))
        audit(db, "update", "price_item", item_id, None, {"code": data["code"], "price": data["price_chf"]})
        return item_id
    new = db.execute(f"INSERT INTO price_items (practice_id, {', '.join(cols)}, created_at) VALUES (?, {', '.join('?' * len(cols))}, ?)",
                     (current_practice_id(), *[data[k] for k in cols], utcnow()))
    audit(db, "create", "price_item", new, None, {"code": data["code"], "price": data["price_chf"]})
    return new


def toggle_item(db: Database, item_id: int) -> None:
    db.execute("UPDATE price_items SET active = 1 - active, updated_at = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), item_id, current_practice_id()))
    audit(db, "toggle", "price_item", item_id)


# --------------------------------------------------------------------------- MWST
def vat_number(db: Database) -> str | None:
    return db.value("SELECT vat_number FROM billing_settings WHERE practice_id = ?", (current_practice_id(),))


def rate_for(db: Database, rate: float) -> float:
    """Ohne MWST-Nummer wird keine MWST verrechnet."""
    return float(rate) if vat_number(db) else 0.0


def vat_in(amount: float, rate: float) -> float:
    """MWST-Anteil eines Endpreises (kaufmännisch gerundet, core/money.py)."""
    return money.vat_in(amount, rate) if rate else 0.0


def vat_summary(lines) -> list[dict]:
    """[{rate, gross, vat}] je Satz > 0 (Zeilen mit «vat_rate» und «amount_chf»)."""
    groups: dict[float, list] = {}
    for ln in lines:
        rate = float(ln["vat_rate"] or 0)
        if rate:
            groups.setdefault(rate, []).append(float(ln["amount_chf"] or 0))
    out = []
    for r, amounts in sorted(groups.items()):
        gross = money.total(amounts)
        out.append({"rate": r, "gross": gross, "vat": vat_in(gross, r)})
    return out


def vat_total(lines) -> float:
    return money.total(x["vat"] for x in vat_summary(lines))
