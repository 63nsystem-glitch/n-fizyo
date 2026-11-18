"""Zahlungseingang aus der Bank: ISO 20022 camt.054 (Gutschriftsanzeige) und camt.053 (Kontoauszug).

Liest nur Daten (kein Netzwerk, keine Entitäten). Jede Einzelbuchung (TxDtls) wird ein Eintrag mit
Betrag, Datum, Referenz (QR-Referenz oder RF…), Name der zahlenden Person, Mitteilung (RmtInf/Ustrd und
Strd/AddtlRmtInf – z. B. «Rechnung 2026-0099», wenn ohne QR-Referenz bezahlt wurde) und eindeutiger Bankreferenz.
Namensraum-unabhängig (camt.054.001.04 … .08, camt.053.001.04 … .08).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from lxml import etree


@dataclass
class Entry:
    amount: float
    currency: str
    credit: bool
    booked_on: str
    reference: str | None
    debtor: str | None
    bank_ref: str
    message: str | None = None


class CamtError(ValueError):
    pass


def _l(el, *names):
    """Erstes Kind-Element über lokale Namen (Pfad)."""
    cur = el
    for n in names:
        if cur is None:
            return None
        cur = next((c for c in cur if isinstance(c.tag, str) and etree.QName(c).localname == n), None)
    return cur


def _all(el, name):
    return [c for c in el.iter() if isinstance(c.tag, str) and etree.QName(c).localname == name]


def _txt(el) -> str | None:
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def parse(data: bytes) -> list[Entry]:
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))
    except etree.XMLSyntaxError as exc:
        raise CamtError("Keine gültige XML-Datei") from exc
    namespace = etree.QName(root).namespace or ""
    if not any(k in namespace for k in ("camt.054", "camt.053")):
        raise CamtError("Keine camt.054- oder camt.053-Datei")
    out: list[Entry] = []
    seen: dict[tuple, int] = {}
    for ntry in _all(root, "Ntry"):
        status = _txt(_l(ntry, "Sts", "Cd")) or _txt(_l(ntry, "Sts"))
        if status and status != "BOOK":
            continue   # vorgemerkt (PDNG), nur Info (INFO) oder künftig (FUTR): kommt später als gebuchte Zeile – sonst doppelt
        ntry_amt = _l(ntry, "Amt")
        ntry_credit = _txt(_l(ntry, "CdtDbtInd")) == "CRDT"
        booked = _txt(_l(ntry, "BookgDt", "Dt")) or (_txt(_l(ntry, "BookgDt", "DtTm")) or "")[:10] or \
            _txt(_l(ntry, "ValDt", "Dt")) or ""
        ntry_ref = _txt(_l(ntry, "AcctSvcrRef")) or ""
        txs = _all(ntry, "TxDtls")
        if not txs:
            amount = float(_txt(ntry_amt) or 0)
            out.append(Entry(amount, ntry_amt.get("Ccy", "CHF") if ntry_amt is not None else "CHF", ntry_credit, booked, None, None,
                             ntry_ref or _hash(seen, booked, amount, None, None)))
            continue
        for tx in txs:
            amt_el = _first(_l(tx, "Amt"), _l(tx, "AmtDtls", "TxAmt", "Amt"), ntry_amt)
            amount = float(_txt(amt_el) or 0)
            ccy = amt_el.get("Ccy", "CHF") if amt_el is not None else "CHF"
            ind = _txt(_l(tx, "CdtDbtInd"))
            credit = (ind == "CRDT") if ind else ntry_credit
            ref = _txt(_l(tx, "RmtInf", "Strd", "CdtrRefInf", "Ref"))
            debtor = _txt(_l(tx, "RltdPties", "Dbtr", "Nm")) or _txt(_l(tx, "RltdPties", "Dbtr", "Pty", "Nm"))
            # Ohne eigene Bankreferenz: aus dem Inhalt (Datum, Betrag, Referenz, Name) – dieselbe Zahlung in der Gutschriftsanzeige
            # (camt.054) und später im Kontoauszug (camt.053) ergibt so dieselbe Kennung und wird nicht zweimal gebucht
            bank_ref = _txt(_l(tx, "Refs", "AcctSvcrRef")) or _hash(seen, booked, amount, ref, debtor)
            out.append(Entry(amount, ccy, credit, booked, ref, debtor, bank_ref, _message(tx)))
    return out


def _message(tx) -> str | None:
    """Freitext der zahlenden Person: alle RmtInf/Ustrd und Strd/AddtlRmtInf, zusammengefügt (höchstens 280 Zeichen)."""
    rmt = _l(tx, "RmtInf")
    if rmt is None:
        return None
    parts = [t for t in (_txt(e) for e in _all(rmt, "Ustrd")) if t] + [t for t in (_txt(e) for e in _all(rmt, "AddtlRmtInf")) if t]
    text = " ".join(" ".join(parts).split())
    return text[:280] or None


def _first(*els):
    """Erstes vorhandene Element (lxml-Elemente ohne Kinder gelten als «falsch» – deshalb nicht «or»)."""
    return next((e for e in els if e is not None), None)


def _hash(seen: dict, booked: str, amount: float, ref: str | None, debtor: str | None) -> str:
    """Kennung aus dem Inhalt; zwei gleiche Zahlungen in derselben Datei (gleicher Tag, Betrag, Referenz) werden durchgezählt."""
    key = (booked, f"{amount:.2f}", (ref or "").replace(" ", "").upper(), (debtor or "").strip().lower())
    seen[key] = seen.get(key, -1) + 1
    return "h:" + hashlib.sha256("|".join(key + (str(seen[key]),)).encode()).hexdigest()[:24]
