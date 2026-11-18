"""Elektronische Rechnung nach Forum Datenaustausch «generalInvoiceRequest» 5.0 (XML).

- Erzeugt Rechnung, Rechnungskopie (Tiers garant: Kopie an den Versicherer), Mahnung oder Storno.
- Jede Datei wird vor dem Speichern/Senden gegen das offizielle, unveränderte Schema geprüft (schemas/).
- Tarifcode (tariff_type): 312 KVG, 311 UV/MV/IV (knowledge/tariffs.py, mit Quelle).
- missing(...) sagt in Klartext-Schlüsseln, welche Stammdaten fehlen (AHV-Nr., GLN, Adressen …).

Vor dem Echtbetrieb mit dem MediData-Testsystem prüfen (modus="test").
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from lxml import etree

from ...core.mail import valid_email
from ...core.timeutil import TZ
from ...core.version import app_version
from ...knowledge import tariffs as catalog
from . import payment

NS = "http://www.forum-datenaustausch.ch/invoice"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_DIR = Path(__file__).with_name("schemas")
# Platzhalter-GLN, wenn eine Partei keine GLN hat (z. B. Patient/in als Rechnungsempfänger bei Tiers garant).
# MediData Schnittstellendokumentation V5.2.0, 3.7.2.1: Spezial-GLN «keine Zustellung» 2000000000008 (nicht im Verzeichnis).
UNKNOWN_GLN = "2000000000008"


class XmlInvalid(Exception):
    """Die erzeugte Datei entspricht nicht dem Schema (Programmfehler oder unvollständige Stammdaten)."""


@dataclass
class Party:
    name: str
    street: str | None = None
    house_no: str | None = None
    zip: str | None = None
    city: str | None = None
    gln: str | None = None
    zsr: str | None = None
    first_name: str | None = None   # gesetzt → Person, sonst Firma
    phone: str | None = None
    email: str | None = None


@dataclass
class Line:
    record: int
    tariff_code: str
    code: str
    name: str
    quantity: float
    date: str
    provider_gln: str
    unit: float
    unit_factor: float
    external_factor: float
    amount: float


@dataclass
class Data:
    """Alles, was die XML-Rechnung braucht – aus der Datenbank zusammengestellt (repo.xml_data)."""
    number: str
    finalized_at: str
    lang: str
    tiers: str               # garant | payant
    law: str
    biller: Party            # Praxis
    provider: Party          # verantwortliche Therapeutin / verantwortlicher Therapeut
    insurer: Party | None
    patient: Party
    patient_sex: str | None  # f | m | x
    patient_birthdate: str | None
    patient_ssn: str | None
    insured_id: str | None
    case_id: str | None
    case_date: str | None
    canton: str | None
    reason: str
    period_from: str
    period_to: str
    diagnosis: str | None
    lines: list[Line]
    total: float
    iban: str | None
    reference: str | None
    referrer: Party | None = None
    paid: float = 0.0
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- Prüfen, was fehlt
CANTONS = ("AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GL", "GR", "JU", "LU", "NE", "NW", "OW", "SG", "SH", "SO", "SZ",
           "TG", "TI", "UR", "VD", "VS", "ZG", "ZH")


def _has(v: str | None) -> bool:
    return bool((v or "").strip())


def missing(d: Data) -> list[str]:
    """Was der Rechnung für ein gültiges XML fehlt – nur Leerzeichen gelten als leer (sonst «Schema verletzt» beim Senden)."""
    out = []
    if not _has(d.biller.name):
        out.append("xml.practice_address")
    if (d.canton or "") not in CANTONS:
        out.append("xml.canton")                 # Kanton der Behandlung (Praxis) – kein stiller Ersatzwert
    if not _gln(d.biller.gln):
        out.append("xml.practice_gln")
    if d.law == "KVG" and not d.biller.zsr:
        out.append("xml.practice_zsr")           # ohne ZSR-Nummer vergütet die Krankenkasse nichts (Grundversicherung)
    if not (_has(d.biller.zip) and _has(d.biller.city)) and "xml.practice_address" not in out:
        out.append("xml.practice_address")
    if not _gln(d.provider.gln) or any(not _gln(ln.provider_gln) for ln in d.lines):
        out.append("xml.therapist_gln")
    if d.insurer is None or not _gln(d.insurer.gln):
        out.append("xml.insurer_gln")
    elif not (_has(d.insurer.name) and _has(d.insurer.zip) and _has(d.insurer.city)):
        out.append("xml.insurer_address")
    if not (_has(d.patient.name) and _has(d.patient.first_name) and _has(d.patient.zip) and _has(d.patient.city)):
        out.append("xml.patient_address")
    if not d.patient_birthdate:
        out.append("xml.patient_birthdate")
    if d.patient_sex not in ("f", "m"):
        out.append("xml.patient_sex")
    if not _ssn(d.patient_ssn):
        out.append("xml.patient_ahv")
    if not d.iban or not payment.valid_iban(d.iban):
        out.append("xml.iban")
    if d.law == "UVG" and not d.case_id:
        out.append("xml.case_id")
    return out


def _gln(v: str | None) -> bool:
    return bool(v) and len(v) == 13 and v.isdigit()


def _ssn(v: str | None) -> bool:
    s = (v or "").replace(".", "").replace(" ", "")
    return len(s) == 13 and s.startswith("756") and s.isdigit()


# --------------------------------------------------------------------------- Aufbau
def _el(parent, tag: str, **attrs):
    return etree.SubElement(parent, f"{{{NS}}}{tag}", **{k: str(v) for k, v in attrs.items() if v is not None and v != ""})


def _text(parent, tag: str, text) -> None:
    _el(parent, tag).text = str(text)


def _cut(v: str | None, n: int) -> str:
    return (v or "").strip()[:n]


def _postal(parent, p: Party) -> None:
    postal = _el(parent, "postal")
    if _has(p.street):
        street = _el(postal, "street", street_name=_cut(p.street, 35), house_no=_cut(p.house_no, 10))
        street.text = _cut(f"{p.street} {p.house_no or ''}", 35)
    _text(postal, "zip", _cut(p.zip, 9))
    _text(postal, "city", _cut(p.city, 35))
    country = _el(postal, "country", country_code="CH")
    country.text = "Schweiz"


def _who(parent, p: Party) -> None:
    if p.first_name:
        person = _el(parent, "person")
        _text(person, "familyname", _cut(p.name, 35))
        _text(person, "givenname", _cut(p.first_name, 35))
        _postal(person, p)
    else:
        company = _el(parent, "company")
        _text(company, "companyname", _cut(p.name, 35))
        _postal(company, p)
    if valid_email(p.email):   # «praxis@» oder Tippfehler: lieber ohne E-Mail als ein ungültiges XML
        holder = parent[-1]
        online = _el(holder, "online")
        _text(online, "email", _cut(p.email, 80))


def _num(v: float) -> str:
    return f"{v:.2f}"


def _factor(v: float) -> str:
    s = f"{v:.4f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _version_int() -> int:
    parts = (app_version().split(".") + ["0", "0"])[:3]
    return int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])


def _local(dt: datetime) -> str:
    """xs:dateTime in Schweizer Zeit mit Offset («2026-11-11T00:30:00+01:00») – gleiches Datum wie auf dem PDF (F06).
    Ohne Offset würde die Uhrzeit in UTC als Ortszeit gelesen: zwischen 00:00 und 01:00/02:00 der Vortag."""
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def build(d: Data, *, modus: str = "test", kind: str = "invoice", reminder: dict | None = None,
          via_gln: str | None = None, guid: str | None = None, to_gln: str | None = None) -> tuple[bytes, str]:
    """kind: invoice | copy | reminder | storno. reminder = {level, date, fee, open}. Gibt (XML, GUID) zurück.
    to_gln: Empfänger für invoice:transport@to – laut MediData-Teilnehmerverzeichnis «glnReceiver», kann von der GLN des
    Versicherers abweichen (Schnittstellendokumentation V5.2.0, 3.6.2). Ohne Angabe: GLN des Versicherers."""
    problems = missing(d)
    if problems:
        raise XmlInvalid(", ".join(problems))
    guid = guid or uuid.uuid4().hex
    lang = d.lang if d.lang in ("de", "fr", "it") else "de"
    root = etree.Element(f"{{{NS}}}request", nsmap={"invoice": NS, "xsi": XSI}, language=lang, modus=modus, guid=guid)
    root.set(f"{{{XSI}}}schemaLocation", f"{NS} generalInvoiceRequest_500.xsd")

    processing = _el(root, "processing")
    transport = _el(processing, "transport", **{"from": d.biller.gln, "to": to_gln if _gln(to_gln) else d.insurer.gln})
    if via_gln:
        _el(transport, "via", via=via_gln, sequence_id=1)

    request_type = "reminder" if kind == "reminder" else "invoice"
    subtype = {"copy": "copy", "storno": "storno"}.get(kind, "normal")
    payload = _el(root, "payload", request_type=request_type, request_subtype=subtype)
    fin = datetime.fromisoformat(d.finalized_at)
    if fin.tzinfo is None:
        fin = fin.replace(tzinfo=timezone.utc)
    _el(payload, "invoice", request_timestamp=int(fin.timestamp()), request_date=_local(fin), request_id=d.number)
    if kind == "reminder" and reminder:
        rdt = datetime.fromisoformat(reminder["date"])
        if rdt.tzinfo is None:
            rdt = rdt.replace(tzinfo=timezone.utc)
        _el(payload, "reminder", request_timestamp=int(rdt.timestamp()), request_date=_local(rdt),
            request_id=f"{d.number}-M{reminder['level']}", reminder_level=reminder["level"])

    body = _el(payload, "body", role="physiotherapist", place="practice")
    prolog = _el(body, "prolog")
    _el(prolog, "generator", name="N-Fizyo", copyright="NSystem GmbH", version=_version_int())

    tiers = _el(body, "tiers_payant" if d.tiers == "payant" else "tiers_garant",
                **({"allowModification": "false"} if d.tiers == "payant" else {}))
    billers = _el(tiers, "billers")
    _who(_el(billers, "biller_gln", gln=d.biller.gln), d.biller)
    if d.biller.zsr:
        _who(_el(billers, "biller_zsr", zsr=d.biller.zsr), d.biller)
    if d.tiers == "payant":
        _who(_el(tiers, "debitor", gln=d.insurer.gln), d.insurer)
    else:
        _who(_el(tiers, "debitor", gln=UNKNOWN_GLN), d.patient)
    providers = _el(tiers, "providers")
    _who(_el(providers, "provider_gln", gln=d.provider.gln, gln_location=d.biller.gln), d.provider)
    if d.provider.zsr:
        _who(_el(providers, "provider_zsr", zsr=d.provider.zsr), d.provider)
    _who(_el(tiers, "insurance", gln=d.insurer.gln), d.insurer)
    sex = "female" if d.patient_sex == "f" else "male"
    _who(_el(tiers, "patient", gender=sex, sex=sex, birthdate=d.patient_birthdate,
             ssn=(d.patient_ssn or "").replace(".", "").replace(" ", "")), d.patient)
    _who(_el(tiers, "guarantor"), d.patient)
    partners = _el(tiers, "partners")
    if d.referrer and _gln(d.referrer.gln):
        _who(_el(partners, "partner", gln=d.referrer.gln, type="referrer"), d.referrer)
    fee = float(reminder["fee"]) if reminder else 0.0
    due = (float(reminder["open"]) + fee) if reminder else d.total
    balance_attrs = {"currency": "CHF", "amount": _num(d.total), "amount_due": _num(due)}
    if reminder:
        balance_attrs["amount_reminder"] = _num(fee)
    if d.tiers != "payant" and d.paid:
        balance_attrs["amount_prepaid"] = _num(d.paid)
    balance = _el(tiers, "balance", **balance_attrs)
    vat = _el(balance, "vat", vat=_num(0))
    _el(vat, "vat_rate", vat_rate="0", amount=_num(d.total), vat=_num(0))

    reason = f"{d.number}"
    if payment.is_qr_iban(d.iban):
        esr = _el(body, "esrQR", iban=d.iban, reference_number=d.reference, payment_reason=reason)
    else:
        esr = _el(body, "esrQRRed", iban=d.iban, reference_number=d.reference, payment_reason=reason)
    _who(_el(esr, "creditor"), d.biller)

    _el(body, "law", type=d.law if d.law in ("KVG", "VVG", "UVG", "IVG", "MVG") else "ORG",
        insured_id=_cut(d.insured_id, 35), case_id=_cut(d.case_id, 35), case_date=(d.case_date or "")[:10])
    treatment = _el(body, "treatment", date_begin=d.period_from[:10], date_end=d.period_to[:10], canton=d.canton,
                    reason=d.reason)
    if d.diagnosis:
        _el(treatment, "diagnosis", type="freetext").text = _cut(d.diagnosis, 350)
    services = _el(body, "services")
    for ln in d.lines:
        _el(services, "service", record_id=ln.record, tariff_type=ln.tariff_code, code=ln.code, name=_cut(ln.name, 350),
            session=1, quantity=_factor(ln.quantity), date_begin=f"{ln.date[:10]}T00:00:00", provider_id=ln.provider_gln,
            responsible_id=d.provider.gln, unit=_factor(ln.unit), unit_factor=_factor(ln.unit_factor),
            external_factor=_factor(ln.external_factor), amount=_num(ln.amount), vat_rate="0")
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    validate(xml)
    return xml, guid


# --------------------------------------------------------------------------- Schema
class _Resolver(etree.Resolver):
    def resolve(self, url, pubid, context):
        for name in ("xmldsig-core-schema.xsd", "xenc-schema.xsd"):
            if url.endswith(name):
                return self.resolve_filename(str(SCHEMA_DIR / name), context)
        return None


@lru_cache(maxsize=1)
def _schema() -> etree.XMLSchema:
    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
    parser.resolvers.add(_Resolver())
    return etree.XMLSchema(etree.parse(str(SCHEMA_DIR / "generalInvoiceRequest_500.xsd"), parser))


def validate(xml: bytes) -> None:
    doc = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True))
    schema = _schema()
    if not schema.validate(doc):
        raise XmlInvalid("; ".join(f"Zeile {e.line}: {e.message}" for e in list(schema.error_log)[:5]))


def modus_of(xml: bytes) -> str | None:
    """Attribut modus (test | production) der Wurzel."""
    return etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True)).get("modus")


def tariff_code(tariff_key: str) -> str:
    t = catalog.TARIFFS.get(tariff_key)
    return t.code if t and t.code else "999"
