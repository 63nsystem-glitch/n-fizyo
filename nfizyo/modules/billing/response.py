"""Antworten der Versicherer auf elektronische Rechnungen: «General Invoice Response» 5.0 (Forum Datenaustausch).

Eine Antwort bezieht sich über invoice/@request_id auf die Rechnungsnummer. Inhalt je nach Tiers:
- tiers_garant:  notification (Mitteilung an Leistungserbringer/Patient)
- tiers_payant / tiers_soldant:  pending (in Bearbeitung, Meldungen) · rejected (Rückweisung mit Fehlern) · accepted (angenommen,
  mit Saldo)
Gelesen werden nur Daten (keine Entitäten, kein Netzwerk). Das Schema prüft N-Fizyo hier nicht: Wir lesen, was der
Versicherer schickt, und zeigen es an – auch wenn die Antwort kleine Abweichungen hat. Verschlüsselte Antworten
(xenc:EncryptedData) werden erkannt, aber nicht entschlüsselt.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from lxml import etree

NS = "http://www.forum-datenaustausch.ch/invoice"
KINDS = ("notification", "pending", "rejected", "accepted")
MAX_FILE = 5 * 1024 * 1024
MAX_FILES = 200
MAX_TOTAL = 50 * 1024 * 1024


class ResponseError(ValueError):
    """Schlüssel unter billing.resp.err_*"""


@dataclass
class Notice:
    code: str
    text: str
    error_value: str | None = None
    valid_value: str | None = None
    record_id: int | None = None


@dataclass
class Response:
    request_id: str
    request_date: str | None
    response_at: str | None
    request_type: str
    request_subtype: str
    reminder_level: int | None
    modus: str
    language: str | None
    guid: str | None
    sender_gln: str | None
    recipient_gln: str | None
    insurer_gln: str | None
    insurer_name: str | None
    patient_name: str | None
    tiers: str
    kind: str
    status_in: str | None = None
    status_out: str | None = None
    explanation: str | None = None
    notices: list[Notice] = field(default_factory=list)
    balance: dict = field(default_factory=dict)
    encrypted: bool = False


def _ln(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _child(el, *names):
    cur = el
    for n in names:
        if cur is None:
            return None
        cur = next((c for c in cur if _ln(c) == n), None)
    return cur


def _children(el, name):
    return [c for c in el if _ln(c) == name] if el is not None else []


def _text(el) -> str | None:
    if el is None:
        return None
    t = " ".join("".join(el.itertext()).split())
    return t or None


def _party_name(el) -> str | None:
    if el is None:
        return None
    company = _child(el, "company", "companyname")
    if company is not None:
        return _text(company)
    person = _child(el, "person")
    if person is not None:
        return " ".join(x for x in (_text(_child(person, "givenname")), _text(_child(person, "familyname"))) if x) or None
    return None


def _float(v) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def parse(data: bytes) -> Response:
    if len(data) > MAX_FILE:
        raise ResponseError("too_big")
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))
    except etree.XMLSyntaxError as exc:
        raise ResponseError("not_xml") from exc
    if _ln(root) != "response":
        raise ResponseError("not_response")
    payload = _child(root, "payload")
    inv = _child(payload, "invoice")
    if payload is None or inv is None or not inv.get("request_id"):
        raise ResponseError("not_response")
    transport = _child(root, "processing", "transport")
    reminder = _child(payload, "reminder")
    ts = payload.get("response_timestamp")
    response_at = None
    if ts and ts.isdigit():
        response_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
    body = _child(payload, "body")
    base = dict(
        request_id=inv.get("request_id").strip(), request_date=inv.get("request_date"), response_at=response_at,
        request_type=payload.get("request_type") or "invoice", request_subtype=payload.get("request_subtype") or "normal",
        reminder_level=int(reminder.get("reminder_level") or 1) if reminder is not None else None,
        modus=root.get("modus") or "production", language=root.get("language"), guid=root.get("guid"),
        sender_gln=transport.get("from") if transport is not None else None,
        recipient_gln=transport.get("to") if transport is not None else None,
    )
    if body is None:
        if _child(payload, "EncryptedData") is not None:
            return Response(**base, insurer_gln=base["sender_gln"], insurer_name=None, patient_name=None, tiers="unknown",
                            kind="pending", encrypted=True)
        raise ResponseError("not_response")
    insurance = _child(body, "insurance")
    patient = _child(body, "patient")
    person = _child(patient, "person")
    out = Response(**base, insurer_gln=insurance.get("gln") if insurance is not None else base["sender_gln"],
                   insurer_name=_party_name(insurance),
                   patient_name=" ".join(x for x in (_text(_child(person, "givenname")), _text(_child(person, "familyname"))) if x) or None,
                   tiers="unknown", kind="pending")
    for tiers in ("tiers_garant", "tiers_payant", "tiers_soldant"):
        t = _child(body, tiers)
        if t is None:
            continue
        out.tiers = tiers.split("_")[1]
        if tiers == "tiers_garant":
            result, out.kind = _child(t, "notification"), "notification"
        else:
            result = next((c for c in t if _ln(c) in ("pending", "rejected", "accepted")), None)
            out.kind = _ln(result) if result is not None else "pending"
        if result is not None:
            out.status_in, out.status_out = result.get("status_in"), result.get("status_out")
            out.explanation = _text(_child(result, "explanation"))
            for n in _children(result, "error") + _children(result, "message"):
                rid = n.get("record_id")
                out.notices.append(Notice(code=n.get("code") or "", text=n.get("text") or "", error_value=n.get("error_value"),
                                          valid_value=n.get("valid_value"), record_id=int(rid) if rid and rid.isdigit() else None))
            bal = _child(result, "balance")
            if bal is not None:
                out.balance = {k: _float(bal.get(k)) for k in ("amount", "amount_reminder", "amount_due", "amount_paid", "amount_delta")
                               if bal.get(k) is not None}
        break
    return out


def files_from_upload(filename: str, data: bytes) -> list[tuple[str, bytes]]:
    """Eine XML-Datei oder ein ZIP mit mehreren (Grenzen gegen übergrosse Archive)."""
    if data[:2] != b"PK":
        return [(filename, data)]
    out: list[tuple[str, bytes]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".xml"):
                    continue
                if len(out) >= MAX_FILES or info.file_size > MAX_FILE or total + info.file_size > MAX_TOTAL:
                    raise ResponseError("zip_too_big")
                with zf.open(info) as fh:
                    content = fh.read(MAX_FILE + 1)
                if len(content) > MAX_FILE:
                    raise ResponseError("zip_too_big")
                total += len(content)
                out.append((info.filename.rsplit("/", 1)[-1][:200], content))
    except zipfile.BadZipFile as exc:
        raise ResponseError("bad_zip") from exc
    if not out:
        raise ResponseError("zip_empty")
    return out
