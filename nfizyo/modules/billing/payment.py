"""Zahlungsangaben: IBAN / QR-IBAN, Referenzen (QR-Referenz oder Creditor Reference ISO 11649), QR-Code-Inhalt.

- QR-IBAN (Institutsidentifikation 30000–31999) → QR-Referenz: 26 Ziffern + Prüfziffer (Modulo 10 rekursiv).
- Normale IBAN → Creditor Reference «RF..» (ISO 11649).
Aus der Referenz lässt sich die Rechnung beim Zahlungseingang wieder finden (camt.054).
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id

_MOD10 = (0, 9, 4, 6, 8, 2, 7, 1, 3, 5)
DEFAULTS = {"iban": None, "payment_days": 30, "reminder_days": 10, "reminder_fee_2": 0.0, "reminder_fee_3": 0.0,
            "xml_modus": "test", "copy_to_insurer": 1, "vat_number": None}


# --------------------------------------------------------------------------- Einstellungen
def settings(db: Database) -> dict:
    row = db.one("SELECT * FROM billing_settings WHERE practice_id = ?", (current_practice_id(),))
    return {**DEFAULTS, **(dict(row) if row else {})}


def save_settings(db: Database, values: dict) -> None:
    allowed = {k: values[k] for k in DEFAULTS if k in values}
    cur = settings(db)
    cur.update(allowed)
    db.execute(
        "INSERT INTO billing_settings (practice_id, iban, payment_days, reminder_days, reminder_fee_2, reminder_fee_3, xml_modus,"
        " copy_to_insurer, vat_number, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(practice_id) DO UPDATE SET iban = excluded.iban,"
        " payment_days = excluded.payment_days, reminder_days = excluded.reminder_days, reminder_fee_2 = excluded.reminder_fee_2,"
        " reminder_fee_3 = excluded.reminder_fee_3, xml_modus = excluded.xml_modus, copy_to_insurer = excluded.copy_to_insurer,"
        " vat_number = excluded.vat_number, updated_at = excluded.updated_at",
        (current_practice_id(), cur["iban"], cur["payment_days"], cur["reminder_days"], cur["reminder_fee_2"], cur["reminder_fee_3"],
         cur["xml_modus"], cur["copy_to_insurer"], cur["vat_number"], utcnow()))


# --------------------------------------------------------------------------- IBAN
def clean_iban(value: str | None) -> str:
    return re.sub(r"\s", "", value or "").upper()


def valid_iban(iban: str) -> bool:
    """Schweiz/Liechtenstein, Prüfsumme nach ISO 13616."""
    if not re.fullmatch(r"(CH|LI)[0-9]{2}[0-9]{5}[0-9A-Z]{12}", iban):
        return False
    moved = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in moved)
    return int(digits) % 97 == 1


def is_qr_iban(iban: str) -> bool:
    return valid_iban(iban) and 30000 <= int(iban[4:9]) <= 31999


# --------------------------------------------------------------------------- Referenzen
def mod10(digits: str) -> int:
    carry = 0
    for ch in digits:
        carry = _MOD10[(carry + int(ch)) % 10]
    return (10 - carry) % 10


def qr_reference(invoice_id: int) -> str:
    base = f"{invoice_id:026d}"
    return base + str(mod10(base))


def scor(body: str) -> str:
    """ISO 11649 Creditor Reference: RF + 2 Prüfziffern + Referenz (max. 21 Zeichen)."""
    numeric = "".join(str(int(ch, 36)) for ch in body.upper() + "RF00")
    return f"RF{98 - int(numeric) % 97:02d}{body.upper()}"


def scor_reference(invoice_id: int) -> str:
    return scor(f"NF{invoice_id}")


def reference_for(iban: str, invoice_id: int) -> str:
    return qr_reference(invoice_id) if is_qr_iban(iban) else scor_reference(invoice_id)


def invoice_id_from_reference(ref: str | None) -> int | None:
    """Aus einer Zahlungsreferenz die interne Rechnungs-ID lesen (nur wenn die Prüfziffer stimmt)."""
    s = re.sub(r"\s", "", ref or "").upper()
    if re.fullmatch(r"[0-9]{27}", s) and mod10(s[:26]) == int(s[26]):
        # Rechnungen der Abrechnung beginnen mit 0; eine andere erste Ziffer gehört einem anderen Modul (9 = Firmenrechnung)
        return (int(s[:26]) or None) if s[0] == "0" and int(s[:26]) < 2 ** 62 else None
    m = re.fullmatch(r"RF([0-9]{2})NF([0-9]{1,18})", s)
    if m and scor_reference(int(m.group(2))) == s:
        return int(m.group(2))
    return None


def money(value) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- QR-Code-Inhalt (Swiss Payment Standards)
def qr_bill(*, iban: str, creditor: dict, debtor: dict | None, amount, reference: str, info: str, lang: str):
    """qrbill prüft alle Felder nach den SIX-Vorgaben und erzeugt den QR-Inhalt (bill.qr_data())."""
    from qrbill import QRBill

    return QRBill(account=iban, creditor=creditor, debtor=debtor, amount=money(amount) if amount is not None else None,
                  reference_number=reference, additional_information=info[:140],
                  language=lang if lang in ("de", "fr", "it", "en") else "de")


def address(name: str, street: str | None, house_no: str | None, zip_code: str | None, city: str | None) -> dict | None:
    if not (name and zip_code and city):
        return None
    return {"name": name[:70], "street": (street or "")[:70], "house_num": (house_no or "")[:16], "pcode": zip_code[:16],
            "city": city[:35], "country": "CH"}
