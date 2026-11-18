"""Testsendungen an die Simulatoren der MediData-Testumgebung (Acceptance-System) – NUR im Testmodus.

Quelle: MediData «Leistungsabrechnung Simulator für Integratoren», Version 2.5 (17.03.2021):
- «Antwortensimulator Leistungsabrechnung», GLN 2099988876514, Gesetze KVG, VVG, OR: erzeugt eine Rechnungsantwort.
  Gesteuert mit dem Kommentar <!-- invoiceresponsegenerator:FLAG --> am Anfang des Dokuments – nie vor dem XML-Prolog.
  Ohne Steuerung (oder mit ungültiger): Antwort «Accepted» (Standardverhalten 2.1.5).
  FLAG für XML 4.4 / 4.5 (Abschnitt 2.1.3): ClinicalDatasetMissing, InvalidSchema, MultipleDelivery, PriorNotice,
  WrongAmountOfMoneyForNonValuedAdditionalCharges, UnknownInvoiceId.
- «Versicherungssimulator», GLN 2099988880030, Gesetze UVG, IVG, MVG: mit <!-- md-dsdt-simulator:success --> wird die Sendung
  innerhalb von 5 Minuten «Erledigt» (beliebige Zeile, nicht vor dem Prolog – N-Fizyo setzt ihn ebenfalls direkt danach).

Achtung XML-Version: Die Beschreibung (2021) nennt Rechnungsantworten nur für generalInvoiceRequest 4.3, 4.4 und 4.5; die
Schnittstellendokumentation V5.2.0 führt GeneralInvoiceRequest_500/Response_500 als unterstützt. Ob der Simulator auf 5.0
antwortet, ist mit MediData zu bestätigen. Für nicht unterstützte Dokumente nennt die Beschreibung entweder die Standardantwort
oder gar keine Antwort (Status «Fehler»). N-Fizyo wartet nie auf eine Antwort: die Abfrage im Hintergrund (medidata_sync, alle
30 min oder «Jetzt abrufen») holt sie ab und übernimmt sie in Abrechnung › Antworten; Status und Benachrichtigungen ebenso.

Die Testrechnung enthält KEINE Patientendaten: eine erfundene Test-Person, dazu die Stammdaten der Praxis (GLN, Adresse, IBAN)
und eine Therapeutin/einen Therapeuten mit GLN. Sie wird nicht als Rechnung gespeichert; das Protokoll (audit_log) hält
Empfänger, Steuerung, Ergebnis und Referenz fest.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ...core.audit import audit
from ...core.db import Database
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import TZ, today
from ...knowledge import tariffs as catalog
from . import medidata_sync, payment, repo, xml500
from .medidata import MediDataClient, UploadResult

ANSWER_GLN = "2099988876514"       # Antwortensimulator Leistungsabrechnung
INSURANCE_GLN = "2099988880030"    # Versicherungssimulator


@dataclass(frozen=True)
class Target:
    key: str
    name: str
    gln: str
    laws: tuple[str, ...]
    law: str                        # Gesetz der Testrechnung
    tariff: str
    code: str
    quantity: float


TARGETS = {
    "antwort": Target("antwort", "Antwortensimulator Leistungsabrechnung", ANSWER_GLN, ("KVG", "VVG", "OR"), "KVG", "kvg312", "7301", 1),
    "versicherung": Target("versicherung", "Versicherungssimulator", INSURANCE_GLN, ("UVG", "IVG", "MVG"), "UVG", "uvmviv", "25.110", 6),
}
# Abschnitt 2.1.3 (XML 4.4 / 4.5); "" = ohne Steuerung → «Accepted»
FLAGS = ("", "ClinicalDatasetMissing", "InvalidSchema", "MultipleDelivery", "PriorNotice",
         "WrongAmountOfMoneyForNonValuedAdditionalCharges", "UnknownInvoiceId")
DSDT_SUCCESS = "<!-- md-dsdt-simulator:success -->"
COMMENT_RX = re.compile(r"<!-- (invoiceresponsegenerator:[A-Za-z]+|md-dsdt-simulator:success) -->")
AUDIT_ACTION = "medidata_simulator"


class SimulatorRefused(Exception):
    """Testsendung nicht erlaubt (nicht im Testmodus, MediData nicht eingerichtet, falsche Eingabe, Stammdaten fehlen)."""

    def __init__(self, key: str, missing: list[str] | None = None):
        super().__init__(key)
        self.key = key
        self.missing = missing or []


def allowed(settings) -> bool:
    """Nur im Testmodus der Box (NFIZYO_MEDIDATA=test). Produktiv nie – auch nicht über die Adresse direkt."""
    return getattr(settings, "medidata_mode", "off") == "test"


def comment_for(target: str, flag: str) -> str | None:
    if target == "versicherung":
        return DSDT_SUCCESS
    if target == "antwort" and flag:
        if flag not in FLAGS:
            raise SimulatorRefused("bad_flag")
        return f"<!-- invoiceresponsegenerator:{flag} -->"
    return None


def insert_after_prolog(xml: bytes, comment: str) -> bytes:
    """Steuer-Kommentar direkt NACH dem XML-Prolog (<?xml …?>) einfügen – nie davor (Simulator-Beschreibung, Fussnote 1)."""
    if not COMMENT_RX.fullmatch(comment):
        raise ValueError("unbekannter Steuer-Kommentar")
    if not xml.startswith(b"<?xml"):
        raise ValueError("XML ohne Prolog")
    end = xml.index(b"?>") + 2
    return xml[:end] + b"\n" + comment.encode("ascii") + xml[end:]


def _ahv_check(digits12: str) -> str:
    """Prüfziffer EAN-13 für eine erfundene AHV-Nummer (756…)."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits12))
    return str((10 - total % 10) % 10)


TEST_AHV = "756000000000" + _ahv_check("756000000000")


def _provider(db: Database):
    """Therapeut/in mit GLN: bevorzugt die angemeldete Person, sonst die erste aktive mit GLN."""
    rows = db.query("SELECT id, name, gln, zsr FROM users WHERE practice_id = ? AND active = 1 AND gln IS NOT NULL AND gln != ''"
                    " ORDER BY id", (current_practice_id(),))
    return next((u for u in rows if u["id"] == current_user_id()), rows[0] if rows else None)


def test_data(db: Database, target: Target, *, now: datetime | None = None) -> xml500.Data:
    """Testrechnung (Tiers payant) an den Simulator: erfundene Test-Person, echte Praxis-Stammdaten, eine Leistung."""
    now = now or datetime.now(timezone.utc)
    pr = repo.practice_row(db)
    u = _provider(db)
    parts = (u["name"] if u else "").split()
    provider = xml500.Party(name=parts[-1] if parts else "", first_name=" ".join(parts[:-1]) or "-", street=pr["street"],
                            house_no=pr["house_no"], zip=pr["zip"], city=pr["city"], gln=u["gln"] if u else None,
                            zsr=u["zsr"] if u else None)
    day = today().isoformat()
    pos = catalog.BY_CODE[(target.tariff, target.code)]
    if target.tariff == "kvg312":
        value = repo.tp_value(db, "kvg312", day) or 1.0
        amounts = {"tp": pos.tp, "tp_value": value, "external_factor": 1.0,
                   "amount_chf": repo.price(target.quantity, pos.tp, value, 1.0)}
    else:
        amounts = repo.amounts(db, target.tariff, pos, target.quantity, day)
    line = xml500.Line(record=1, tariff_code=xml500.tariff_code(target.tariff), code=pos.code, name=pos.label_de,
                       quantity=target.quantity, date=day, provider_gln=provider.gln or "", unit=amounts["tp"],
                       unit_factor=amounts["tp_value"], external_factor=amounts["external_factor"], amount=amounts["amount_chf"])
    iban = payment.clean_iban(payment.settings(db)["iban"])
    number = f"SIM-{now.astimezone(TZ):%Y%m%d-%H%M%S}"     # Ortszeit Zürich, gut lesbar
    return xml500.Data(
        number=number, finalized_at=now.replace(microsecond=0).isoformat(), lang=pr["default_lang"], tiers="payant", law=target.law,
        biller=repo._party_practice(pr), provider=provider,
        insurer=xml500.Party(name=target.name, zip="0000", city="Test", gln=target.gln),
        patient=xml500.Party(name="Simulator", first_name="Test", street="Testweg", house_no="1", zip="0000", city="Test"),
        patient_sex="m", patient_birthdate="1970-01-01", patient_ssn=TEST_AHV, insured_id="TEST-0001",
        case_id="TEST-0001" if target.law == "UVG" else None, case_date=day if target.law == "UVG" else None,
        canton=(pr["canton"] or "").strip().upper() or None, reason="accident" if target.law == "UVG" else "disease",
        period_from=day, period_to=day, diagnosis=None, lines=[line], total=line.amount, iban=iban,
        reference=payment.reference_for(iban, 0) if iban else None)


def build(db: Database, target_key: str, flag: str, *, now: datetime | None = None) -> tuple[bytes, str, xml500.Data]:
    """XML der Testrechnung (modus="test") mit Steuer-Kommentar nach dem Prolog, gegen das Schema geprüft."""
    target = TARGETS.get(target_key)
    if target is None:
        raise SimulatorRefused("bad_target")
    comment = comment_for(target_key, flag)
    data = test_data(db, target, now=now)
    problems = xml500.missing(data)
    if problems:
        raise SimulatorRefused("missing", problems)
    reach = medidata_sync.reach(db, target.gln)                 # glnReceiver laut Teilnehmerverzeichnis, falls vorhanden
    xml, guid = xml500.build(data, modus="test", kind="invoice", to_gln=reach["receiver"] if reach and reach.get("listed") else None)
    if comment:
        xml = insert_after_prolog(xml, comment)
        xml500.validate(xml)
    return xml, guid, data


async def send(settings, db: Database, target_key: str, flag: str) -> tuple[UploadResult, str]:
    """Testrechnung an den Simulator senden. Wirft SimulatorRefused, wenn nicht erlaubt."""
    if not allowed(settings):
        raise SimulatorRefused("not_test")
    md = MediDataClient(settings)
    if md.mode != "test" or not medidata_sync.active_here(db, settings):
        raise SimulatorRefused("not_test" if md.mode != "test" else "not_configured")
    flag = flag if target_key == "antwort" else ""
    xml, guid, data = build(db, target_key, flag)
    if xml500.modus_of(xml) != "test":                          # doppelte Sicherung: nie modus="production"
        raise SimulatorRefused("not_test")
    info = md.info(modus="test", document_reference=guid, correlation_reference=data.number)
    result = await md.upload(xml, f"{data.number}.xml", info)
    audit(db, AUDIT_ACTION, "billing_settings", current_practice_id(), None,
          {"target": target_key, "flag": flag, "number": data.number, "status": result.status, "reference": result.reference,
           "error": (result.error or "")[:200] or None})
    return result, data.number


def recent(db: Database, limit: int = 10) -> list[dict]:
    """Letzte Testsendungen dieser Praxis (aus dem Protokoll)."""
    out = []
    for r in db.query("SELECT detail, at FROM audit_log WHERE practice_id = ? AND action = ? ORDER BY id DESC LIMIT ?",
                      (current_practice_id(), AUDIT_ACTION, limit)):
        try:
            d = json.loads(r["detail"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append({**d, "at": r["at"]})
    return out
