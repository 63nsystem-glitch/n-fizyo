"""MediData-Webservice «Leistungsabrechnung» (eLA): Versand, Teilnehmerverzeichnis, Status, Abholen, Benachrichtigungen.

Grundlage (geprüft, CONFIRMED = True) – nur offizielle MediData-Dokumente:
  «Leistungsabrechnung – Schnittstellendokumentation (de)» V5.2.0 vom 02.09.2026 (Titelblatt «5.1.1», Fusszeile «V 5.1»):
    3.6 Teilnehmerverzeichnis  GET  {base}/ela/participants          name, glnparticipant, lawtype (1–6), limit (0–1000,
                                                                    Standard 50), offset; weitere Seiten über Link rel="next"
    3.7 Dokument versenden     POST {base}/ela/uploads               multipart/form-data: «elauploadinfo» (info.json,
                                                                    application/json, optional) + «elauploadstream» (XML,
                                                                    application/octet-stream) → 201 {"transmissionReference"};
                                                                    409 = gleiche Sendung innerhalb einer Minute (Referenz der ersten)
    3.8 Dokumentenstatus       GET  {base}/ela/uploads/{ref}/status  (+ /status/detail für Teilauslieferungen); 404 = unbekannt
    3.9 Dokumente abholen      GET  {base}/ela/downloads (limit, Standard 10; Header X-ELA-PENDINGDOCUMENTS),
                               GET  {base}/ela/downloads/{ref} mit Accept: application/octet-stream,
                               PUT  {base}/ela/downloads/{ref}/status {"status": "CONFIRMED"}
    3.10 Benachrichtigungen    GET  {base}/ela/notifications (from, to, limit, offset; Link rel="next"),
                               PUT  {base}/ela/notifications/{id}/status {"notificationFetched": true}
    Fehlercodes (Kap. «Fehlerbehandlung»); Anhang BestPractice: gegenseitiges TLS (Client-Zertifikat) + «Authorization: Basic»
    + X-CLIENT-ID (Master_Subclient, 10_10 Ziffern), «Expect: 100-continue» beim Upload (sonst HTTP 413 bei grossen Dateien),
    Abfrageintervalle (participants 24 h; status, downloads, notifications 30 min).
  «Client Schnittstellen» V1.5 (09.05.2022): allgemeine HTTP-Codes (2.5), Fehler-JSON {"errors": [{code, userMessage, …}]} (3.3),
    Box-Zugang https://<box>:8100/md/ mit selbstsigniertem Serverzertifikat (3.1.1).
  «Best Practice Webservice MediData» V1.2 (03.06.2024): dieselben Aufrufe, Beispiele mit «Authorization: Basic …»; Pfad
    …/md/srv/id<N>/ – die Nummer N legt MediData je Kunde fest (V5.2.0 zeigt id0).

Basis-URL (NFIZYO_MEDIDATA_URL, alles bis vor «/ela/»): Webservice https://services.medidata.ch/md/srv/id<N> (produktiv) bzw.
https://services-acc.medidata.ch/md/srv/id<N> (Test/Acceptance); Box/VA https://<box>:8100 (→ …/md, Client Schnittstellen 3.1.1).
Offen bei MediData: V5 beschreibt nur noch den Webservice; die eLA-Pfade der Box/VA werden unter …/md gleich angenommen.

Ergebnis «unklar» (status «unknown»): Zeitüberschreitung, Abbruch während der Sendung, 5xx oder 2xx ohne transmissionReference.
Es wird NIE automatisch nochmals gesendet. Geklärt wird später automatisch (Benachrichtigung mit documentReference/
transmissionReference, Statusabfrage, Antwort des Versicherers mit correlationReference) oder von der Leitung im Portal.
Eindeutig abgelehnt (status «failed», darf nach Korrektur neu gesendet werden): 4xx ausser 409, keine Verbindung aufgebaut,
Client-Zertifikat unbrauchbar.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from . import medidata_cert

CONFIRMED = True             # Schnittstelle laut Schnittstellendokumentation V5.2.0 umgesetzt (siehe oben)
DOC = "MediData Leistungsabrechnung Schnittstellendokumentation V5.2.0"

NO_DELIVERY_GLN = "2000000000008"    # 3.7.2.1 Spezial-GLN «keine Zustellung»
# MIME-Type unserer Rechnung (generalInvoiceRequest 5.0). V5.2.0 nennt «GeneralInvoiceRequest_500» ohne MIME-Type; abgeleitet
# aus dem Muster der übrigen Typen (Prozessbeschrieb V3.7: application/x-fd-geninv-req-v45+xml; V5.2.0-Beispiele …-v44/-v43).
# Für «notSupported» gelten darum «…geninv-req-v50+xml» und «…-v500+xml». → Frage an MediData.
MIME_REQUEST = "application/x-fd-geninv-req-v50+xml"
MIME_RX = {"5.0": re.compile(r"geninv-req-v50{1,2}\+xml", re.I), "4.5": re.compile(r"geninv-req-v450?\+xml", re.I)}
CLIENT_ID_RX = re.compile(r"\d{10}_\d{10}")   # Anhang BestPractice: «10 digits Master_10 digits Subclient»

UPLOAD_TIMEOUT = 60          # s für den Versand (Server bricht unvollständige Anfragen nach 30 s mit 408 ab)
POLL_TIMEOUT = 30            # s für Abfragen (Server-Standard 30 s, Client Schnittstellen 2.5)
MAX_DOCUMENT = 25 * 1024 * 1024
REF_RX = re.compile(r"[A-Za-z0-9-]{1,64}")
CODE_RX = re.compile(r"\b(?:UPLOAD|DOWNLOAD|STATUS|CONFIRMATION|PARTICIPANT|PROCESSING):[A-Z0-9_-]+")
UUID_RX = re.compile(r"[0-9a-zA-Z]{8,10}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{12}")

# Fehlercodes laut Kapitel «Fehlerbehandlung» (Strings, können sich ändern – unbekannte werden roh angezeigt).
# Text in de/fr: billing.mderr.<CODE mit «_» statt «:»>
KNOWN_CODES = (
    "UPLOAD:WRONG-SENDER-GLN-IN-XML", "UPLOAD:WRONG-INTERMEDIATE-GLN-IN-XML", "UPLOAD:XML-NOT-VALID", "UPLOAD:UNKNOWN-GLN",
    "UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION", "UPLOAD:METADATA-EXTRACTION-FAILED", "UPLOAD:INVALID-ENCODING",
    "UPLOAD:COMMUNICATION-ERROR", "UPLOAD:SUBSCRIPTION-LOCKED", "UPLOAD:UNKNOWN-ERROR", "UPLOAD:PARAMETER-MISMATCH",
    "UPLOAD:INVALID-PARAMETER-VALUE", "UPLOAD:DUPLICATED-DOCUMENT", "UPLOAD:RETRY-TIMEOUT-EXPIRED", "UPLOAD:REFERENCE-NOT-FOUND",
    "UPLOAD:DOCUMENT-NOT-ACCEPTED-BY-RECEIVER", "UPLOAD:INVALID-CONTEXT-FOR-DOCUMENT", "UPLOAD:DOCUMENT-SIZE-EXCEEDED",
    "UPLOAD:TG-NOT-ACCEPTED-BY-RECEIVER", "UPLOAD:DELIVERY-TO-PATIENT-NOT-ALLOWED", "UPLOAD:NO-RECEIVER-DEFINED",
    "UPLOAD:INVALID-TRUSTCENTER", "UPLOAD:WRONG-MODUS", "UPLOAD:POSTAL-ADDRESS-INCOMPLETE",
    "UPLOAD:DELIVERY-TO-PATIENTPORTAL-INVALID-ADDRESS", "UPLOAD:RECEIVER-NOT-REACHABLE", "UPLOAD:MIMETYPE-NOT-SENDABLE-TO-RECEIVER",
    "DOWNLOAD:FILE-NOT-FOUND", "DOWNLOAD:UNKNOWN-ERROR", "DOWNLOAD:REFERENCE-NOT-FOUND", "DOWNLOAD:INVALID-PARAMETER-VALUE",
    "DOWNLOAD:DELIVERY-FAILED", "STATUS:UNKNOWN-ERROR", "CONFIRMATION:UNKNOWN-ERROR", "PARTICIPANT:INVALID-PARAMETER-VALUE",
    "PROCESSING:MODUS-NOT-ACCEPTED-BY-RECEIVER", "PROCESSING:DELIVERY-TO-TRUSTCENTER-VIA-TRUSTX-FAILED",
    "PROCESSING:DELIVERY-TO-TRUSTCENTER-VIA-HPLUS-FAILED", "PROCESSING:DELIVERY-TO-TRUSTCENTER-VIA-NEWINDEX-FAILED",
    "PROCESSING:DELIVERY-TO-TRUSTCENTER-VIA-MEDPOINT-FAILED", "PROCESSING:DELIVERY-TO-TRUSTCENTER-VIA-DSDT-FAILED",
)
# Allgemeine HTTP-Codes (Client Schnittstellen V1.5, 2.5) → billing.mdhttp.<code>
HTTP_CODES = (400, 401, 402, 403, 404, 405, 406, 408, 411, 413, 414, 415, 429, 500)


def code_key(code: str | None) -> str | None:
    """i18n-Schlüssel für einen MediData-Fehlercode (None = unbekannt → Code roh anzeigen)."""
    if not code:
        return None
    if code.startswith("UPLOAD:EBILL-"):
        return "billing.mderr.UPLOAD_EBILL"
    return "billing.mderr." + code.replace(":", "_") if code in KNOWN_CODES else None


def error_details(body: bytes | str) -> tuple[list[str], str]:
    """(Fehlercodes, kurze Meldung) aus einer Fehlerantwort: {"errors": [{"code", "userMessage", "internalMessage"}]}
    (Client Schnittstellen 3.3) oder Code irgendwo im Text."""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else (body or "")
    codes: list[str] = []
    message = ""
    try:
        data = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        data = None
    items = data.get("errors") if isinstance(data, dict) and isinstance(data.get("errors"), list) else [data] if isinstance(data, dict) else []
    for e in items:
        if isinstance(e, dict):
            for k in ("code", "errorCode"):
                if isinstance(e.get(k), str) and e[k] and e[k] not in codes:
                    codes.append(e[k])
            message = message or str(e.get("userMessage") or e.get("internalMessage") or e.get("message") or "")
    for c in CODE_RX.findall(text):
        if c not in codes:
            codes.append(c)
    return codes, (message or ("" if data is not None else text))[:200]


def base_url(settings) -> str:
    """Alles bis vor «/ela/»: Webservice …/md/srv/<id>, Box/VA …:8100/md."""
    url = (settings.medidata_url or "").rstrip("/")
    return url if urlsplit(url).path.startswith("/md") else url + "/md"


def is_webservice(settings) -> bool:
    return "/md/srv/" in urlsplit((settings.medidata_url or "") + "/").path


def client_id_ok(settings) -> bool:
    """X-CLIENT-ID im Format des Webservice (1000xxxxxx_1200yyyyyy). Box/VA: eigene, kürzere IDs – nicht geprüft."""
    return not is_webservice(settings) or bool(CLIENT_ID_RX.fullmatch(settings.medidata_client_id or ""))


def _ref(ref: str) -> str:
    if not REF_RX.fullmatch(ref or ""):
        raise MdError("Ungültige transmissionReference")
    return quote(ref, safe="")


@dataclass
class UploadResult:
    ok: bool
    reference: str | None = None
    error: str | None = None
    unclear: bool = False           # vielleicht angekommen – nicht automatisch neu senden
    error_code: str | None = None
    http_status: int | None = None
    duplicate: bool = False         # 409: dieselbe Sendung kam innerhalb der letzten Minute schon an (Referenz der ersten)

    @property
    def status(self) -> str:
        return "sent" if self.ok else ("unknown" if self.unclear else "failed")

    @property
    def resolution(self) -> str | None:
        return "duplicate" if self.duplicate else None


class MdError(Exception):
    """Abfrage fehlgeschlagen (nichts geändert). status = HTTP-Code oder None (keine Verbindung)."""

    def __init__(self, message: str, *, status: int | None = None, codes: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.codes = codes or []


@dataclass
class Reply:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    next_url: str | None = None

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


class MediDataClient:
    def __init__(self, settings):
        self.s = settings

    @property
    def mode(self) -> str:
        return self.s.medidata_mode if self.s.medidata_mode in ("off", "test", "production") else "off"

    @property
    def webservice(self) -> bool:
        return is_webservice(self.s)

    @property
    def configured(self) -> bool:
        """Zugang vollständig. Webservice: zusätzlich Client-Zertifikat (gegenseitiges TLS, Anhang BestPractice)."""
        base = bool(self.s.medidata_url and self.s.medidata_client_id and self.s.medidata_user and self.s.medidata_password)
        return self.mode != "off" and base and (not self.webservice or bool(self.s.medidata_cert and self.s.medidata_key))

    @property
    def base(self) -> str:
        return base_url(self.s)

    def auth_headers(self) -> dict:
        auth = base64.b64encode(f"{self.s.medidata_user}:{self.s.medidata_password}".encode()).decode()
        return {"X-CLIENT-ID": self.s.medidata_client_id, "Authorization": f"Basic {auth}"}

    def info(self, *, modus: str, document_reference: str, correlation_reference: str | None = None,
             to_patient: bool = False) -> dict:
        """Steuerdatei «elauploadinfo» (3.7.2 «Die Steuerparameter im Detail»).

        - modus: muss dem Attribut modus im XML entsprechen (sonst Fehler).
        - toOrganization: weggelassen – das XML-Attribut «to» gilt (glnReceiver aus dem Teilnehmerverzeichnis); jede andere
          Abweichung als 2000000000008 wäre ein Fehler (PARAMETER-MISMATCH).
        - toPatient false: N-Fizyo gibt Patient/innen die Kopie bzw. Rechnung selbst (PDF/E-Mail); MediData soll nicht
          zusätzlich drucken oder zustellen (Kosten). Gleich wie im XML (print_copy_to_guarantor fehlt = false) und
          übersteuert die Voreinstellung im Kundenportal.
        - toTrustCenter, printLanguage, postalDelivery, forcePostalToPatient: weggelassen (Kundenportal bzw. kein Druck).
        - documentReference: eindeutig je Sendung (guid) → Zuordnung von Benachrichtigungen.
        - correlationReference: Geschäftsfall = Rechnungsnummer → Antworten des Versicherers tragen sie ebenfalls."""
        out = {"modus": "production" if modus == "production" else "test", "toPatient": bool(to_patient),
               "documentReference": document_reference[:100]}
        if correlation_reference:
            out["correlationReference"] = correlation_reference[:100]
        return out

    def _ctx(self):
        return medidata_cert.client_ssl_context(self.s)

    # ------------------------------------------------------------------ Versand (3.7)
    async def upload(self, xml: bytes, filename: str, info: dict) -> UploadResult:
        """Genau EIN Versuch (nie automatisch wiederholen). Antwort → sent | failed | unknown (siehe Modulbeschreibung)."""
        if not self.configured:
            return UploadResult(False, error="MediData ist in dieser Box nicht eingerichtet.")
        try:
            ctx = self._ctx()
        except medidata_cert.CertError as exc:
            return UploadResult(False, error=f"Client-Zertifikat: {exc}"[:300])
        import aiohttp

        form = aiohttp.FormData()                       # Reihenfolge und Teile wie im Beispiel 3.7.4
        form.add_field("elauploadinfo", json.dumps(info), filename="info.json", content_type="application/json")
        form.add_field("elauploadstream", xml, filename=filename, content_type="application/octet-stream")
        headers = {**self.auth_headers(), "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)) as session:
                # «Expect: 100-continue» (Anhang BestPractice, «Common Errors»): der Inhalt geht erst nach «100 Continue»
                async with session.post(f"{self.base}/ela/uploads", data=form, headers=headers,
                                        ssl=ctx if ctx is not None else True, expect100=True, allow_redirects=False) as resp:
                    status, body = resp.status, await resp.content.read(100_000)
        except aiohttp.ClientConnectorError as exc:          # keine Verbindung aufgebaut: sicher nicht gesendet
            return UploadResult(False, error=f"{type(exc).__name__}: {exc}"[:300])
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exc:
            return UploadResult(False, error=f"Zeitüberschreitung ({type(exc).__name__}) – Stand unklar", unclear=True)
        except Exception as exc:  # noqa: BLE001 - Abbruch während der Sendung: vielleicht angekommen
            return UploadResult(False, error=f"{type(exc).__name__}: {exc}"[:300], unclear=True)
        return classify(status, body)

    # ------------------------------------------------------------------ Abfragen (3.6, 3.8, 3.9, 3.10)
    def session(self):
        """Eine Sitzung für eine Abfragerunde (nacheinander, nie parallel)."""
        import aiohttp

        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT), headers=self.auth_headers())

    async def call(self, session, method: str, url: str, *, ctx, accept: str = "application/json", payload=None,
                   limit: int = 5_000_000) -> Reply:
        import aiohttp

        if not url.startswith(self.base + "/"):
            raise MdError("Adresse ausserhalb des MediData-Zugangs")       # Link-Header nie auf fremde Server folgen
        headers = {"Accept": accept}
        try:
            async with session.request(method, url, headers=headers, json=payload, ssl=ctx if ctx is not None else True,
                                       allow_redirects=False) as resp:
                body = await resp.content.read(limit + 1)
                nxt = resp.links.get("next")
                reply = Reply(resp.status, body, dict(resp.headers), str(nxt.get("url")) if nxt else None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise MdError(f"MediData nicht erreichbar ({type(exc).__name__})") from None
        if len(reply.body) > limit:
            raise MdError("Antwort zu gross", status=reply.status)
        if reply.status >= 400 and reply.status != 404:
            codes, message = error_details(reply.body)
            raise MdError(f"MediData {reply.status}" + (f" {codes[0]}" if codes else "") + (f": {message}" if message else ""),
                          status=reply.status, codes=codes)
        return reply

    def _next(self, reply: Reply) -> str | None:
        """Link rel="next" – nur wenn er beim eigenen Zugang bleibt; sonst Seiten über offset (beides dokumentiert)."""
        if not reply.next_url:
            return None
        url = _absolute(self.base, reply.next_url)
        return url if url.startswith(self.base + "/") else None

    async def participants(self, session, *, ctx, page_size: int = 1000, max_pages: int = 50, lawtype: int | None = None,
                           gln: str | None = None) -> list[dict]:
        """3.6: alle Seiten; «next» (Header Link) folgen, bis keiner mehr kommt. Ohne Link aber volle Seite: offset.
        lawtype (1 KVG … 6 ORG) schränkt ein – laut 3.6.1 «in der Regel bei Kostenträgern relevant, jedoch nicht für
        Leistungserbringer»: die tägliche Abfrage holt darum alle und filtert nach Gesetz erst in N-Fizyo (lawTypes)."""
        if not 0 < page_size <= 1000:
            raise MdError("limit: 1–1000")
        query = f"limit={page_size}"
        if lawtype is not None:
            if lawtype not in LAW_CODES:
                raise MdError("lawtype: 1–6")
            query += f"&lawtype={lawtype}"
        if gln:
            if not re.fullmatch(r"\d{13}", gln):
                raise MdError("glnparticipant: 13 Ziffern")
            query += f"&glnparticipant={gln}"
        url = f"{self.base}/ela/participants?{query}"
        out: dict[tuple, dict] = {}
        offset = 0
        for _ in range(max_pages):
            reply = await self.call(session, "GET", url, ctx=ctx)
            if reply.status != 200 or not isinstance(reply.json(), list):
                raise MdError(f"Teilnehmerverzeichnis: unerwartete Antwort {reply.status}", status=reply.status)
            page = reply.json()
            before = len(out)
            for p in page:
                item = parse_participant(p)
                if item:
                    out[(item["gln_participant"], item["gln_receiver"])] = item
            nxt = self._next(reply)
            if nxt:
                url = nxt
            elif len(page) >= page_size and len(out) > before:
                offset += page_size
                url = f"{self.base}/ela/participants?{query}&offset={offset}"
            else:
                break
        else:
            raise MdError("Teilnehmerverzeichnis: zu viele Seiten – abgebrochen, altes Verzeichnis bleibt")
        return list(out.values())

    async def upload_status(self, session, ref: str, *, ctx, detail: bool = False) -> dict | None:
        """3.8 / 3.8.4. None = 404 (Referenz gehört zu keinem versandten Dokument)."""
        reply = await self.call(session, "GET", f"{self.base}/ela/uploads/{_ref(ref)}/status" + ("/detail" if detail else ""),
                                ctx=ctx)
        if reply.status == 404:
            return None
        data = reply.json()
        if reply.status != 200 or not isinstance(data, dict) or data.get("status") not in ("DONE", "PROCESSING", "ERROR"):
            raise MdError(f"Status: unerwartete Antwort {reply.status}", status=reply.status)
        return data

    async def downloads(self, session, *, ctx, limit: int = 50) -> tuple[list[dict], int | None]:
        """3.9.2: Liste pendenter Dokumente (Reihenfolge nicht garantiert) + X-ELA-PENDINGDOCUMENTS."""
        reply = await self.call(session, "GET", f"{self.base}/ela/downloads?limit={limit}", ctx=ctx)
        data = reply.json()
        if reply.status != 200 or not isinstance(data, list):
            raise MdError(f"Downloads: unerwartete Antwort {reply.status}", status=reply.status)
        pending = next((v for k, v in reply.headers.items() if k.lower() == "x-ela-pendingdocuments"), None)
        try:
            pending_n = int(pending) if pending is not None else None
        except ValueError:
            pending_n = None
        return [d for d in data if isinstance(d, dict) and REF_RX.fullmatch(str(d.get("transmissionReference") or ""))], pending_n

    async def download(self, session, ref: str, *, ctx) -> bytes | None:
        """3.9.3: Originaldokument (Accept application/octet-stream). None = 404."""
        reply = await self.call(session, "GET", f"{self.base}/ela/downloads/{_ref(ref)}", ctx=ctx, accept="application/octet-stream",
                                limit=MAX_DOCUMENT)
        if reply.status == 404:
            return None
        if reply.status != 200:
            raise MdError(f"Download: unerwartete Antwort {reply.status}", status=reply.status)
        return reply.body

    async def confirm_download(self, session, ref: str, *, ctx) -> bool:
        """3.9.4: PUT {"status": "CONFIRMED"}. True = bestätigt (404: nichts mehr zu bestätigen – gilt als erledigt)."""
        reply = await self.call(session, "PUT", f"{self.base}/ela/downloads/{_ref(ref)}/status", ctx=ctx, payload={"status": "CONFIRMED"})
        return reply.status in (200, 204, 404)

    async def notifications(self, session, *, ctx, since: str | None = None, page_size: int = 100, max_pages: int = 10) -> list[dict]:
        """3.10.1: alle Seiten (Link rel="next", sonst offset). since = «from» (ISO 8601 Date, JJJJ-MM-TT)."""
        if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            raise MdError("from: ISO-Datum JJJJ-MM-TT")
        query = (f"from={since}&" if since else "") + f"limit={page_size}"
        url = f"{self.base}/ela/notifications?{query}&offset=0"
        out: dict[int, dict] = {}
        offset = 0
        for _ in range(max_pages):
            reply = await self.call(session, "GET", url, ctx=ctx)
            data = reply.json()
            if reply.status != 200 or not isinstance(data, list):
                raise MdError(f"Benachrichtigungen: unerwartete Antwort {reply.status}", status=reply.status)
            before = len(out)
            for n in data:
                if isinstance(n, dict) and str(n.get("id", "")).isdigit():
                    out[int(n["id"])] = n
            nxt = self._next(reply)
            if nxt:
                url = nxt
            elif len(data) >= page_size and len(out) > before:
                offset += page_size
                url = f"{self.base}/ela/notifications?{query}&offset={offset}"
            else:
                break
        return list(out.values())

    async def confirm_notification(self, session, nid: int, *, ctx) -> bool:
        """3.10.2: PUT {"notificationFetched": true}. 404 = keine solche Benachrichtigung (gilt als erledigt)."""
        reply = await self.call(session, "PUT", f"{self.base}/ela/notifications/{int(nid)}/status", ctx=ctx,
                                payload={"notificationFetched": True})
        return reply.status in (200, 204, 404)


def classify(status: int, body: bytes) -> UploadResult:
    """HTTP-Antwort auf POST …/ela/uploads einordnen (3.7.5; allgemeine Codes: Client Schnittstellen 2.5)."""
    data = _json(body)
    ref = _reference(data, body)
    if status in (200, 201):
        if not ref:                                         # angenommen, aber ohne Referenz: nicht nachverfolgbar
            return UploadResult(False, error=f"MediData {status} ohne transmissionReference – Stand unklar", unclear=True,
                                http_status=status)
        return UploadResult(True, reference=ref, http_status=status)
    if status == 409:                                       # gleiche Sendung innerhalb einer Minute: Referenz der ersten
        if not ref:
            return UploadResult(False, error="MediData 409 ohne transmissionReference – Stand unklar", unclear=True,
                                http_status=409, error_code="UPLOAD:DUPLICATED-DOCUMENT")
        return UploadResult(True, reference=ref, duplicate=True, http_status=409, error_code="UPLOAD:DUPLICATED-DOCUMENT")
    codes, message = error_details(body)
    code = codes[0] if codes else None
    text = f"MediData {status}" + (f" {code}" if code else "") + (f": {message}" if message else "")
    if status >= 500:                                       # Server hat vielleicht schon verarbeitet
        return UploadResult(False, error=(text + " – Stand unklar")[:300], unclear=True, error_code=code, http_status=status)
    # 4xx (auch 408 unvollständig, 413 zu gross/ohne Anmeldung) und 3xx (Weiterleitung, nicht befolgt): nicht angenommen
    return UploadResult(False, error=text[:300], error_code=code, http_status=status)


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _reference(data, body: bytes) -> str | None:
    """transmissionReference aus der Antwort (bei 409 evtl. in einer Fehlerantwort → als UUID im Text)."""
    if isinstance(data, dict) and isinstance(data.get("transmissionReference"), str) and REF_RX.fullmatch(data["transmissionReference"]):
        return data["transmissionReference"]
    m = UUID_RX.search(body.decode("utf-8", "replace")) if body else None
    return m.group(0) if m else None


def _absolute(base: str, url: str) -> str:
    """Link rel="next": relativ oder absolut – muss beim selben Server bleiben (sonst MdError in call())."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}{url if url.startswith('/') else '/' + url}"


def parse_participant(p) -> dict | None:
    """Ein Eintrag des Teilnehmerverzeichnisses (3.6.2) → Felder der Tabelle. Ungültige Einträge werden übersprungen."""
    if not isinstance(p, dict):
        return None
    gp, gr = str(p.get("glnParticipant") or ""), str(p.get("glnReceiver") or "")
    if not (re.fullmatch(r"\d{13}", gp) and re.fullmatch(r"\d{13}", gr)):
        return None

    def _int(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    laws = [x for x in (p.get("lawTypes") or []) if isinstance(x, int) and 1 <= x <= 6]
    ns = [str(x)[:120] for x in (p.get("notSupported") or []) if isinstance(x, str)]
    return {"gln_participant": gp, "gln_receiver": gr, "name": str(p.get("name") or gp)[:200], "street": _s(p.get("street")),
            "zip": _s(p.get("zipCode"), 20), "town": _s(p.get("town")), "law_types": laws, "bag_number": _int(p.get("bagNumber")),
            "tg_tp_change": bool(p.get("tgTpChange")), "additional_costs": bool(p.get("additionalCosts")),
            "max_receive": _int(p.get("maxReceive")), "not_supported": ns, "tg_allowed": bool(p.get("tgAllowed"))}


def _s(v, n: int = 200) -> str | None:
    return str(v)[:n] if isinstance(v, str) and v.strip() else None


def accepts(not_supported: list[str], version: str) -> bool:
    """Nimmt der Teilnehmer generalInvoiceRequest dieser Version an? (Liste «notSupported», 3.6.2)"""
    rx = MIME_RX[version]
    return not any(rx.search(m) for m in not_supported or [])


LAW_CODES = {1: "KVG", 2: "UVG", 3: "IVG", 4: "MVG", 5: "VVG", 6: "ORG"}     # 3.6.1 «Mögliche Gesetze»


# --------------------------------------------------------------------------- Neue X-Client-ID (Box/VA → Webservice)
class MigrationError(Exception):
    pass


MIGRATION_ERRORS = {
    400: "Anfrage ungültig – cloudclientinstanceid prüfen (MediData gibt sie bei der Migration bekannt).",
    403: "Nicht berechtigt – Benutzer/Passwort (Kundenportal › Einstellungen › Infrastruktur › Branchenapplikation › Credentials) "
         "und die alte X-Client-ID prüfen.",
    500: "Interner Fehler bei MediData – später nochmals versuchen.",
}


async def new_client_id(settings, cloud_instance_id: str, *, timeout: float = 30) -> str:
    """MediData «Requesting the new X-Client-ID» V1.1: GET <Box|VA>/md/cloudmigration?cloudclientinstanceid=<id>
    mit Accept: application/json, Basic-Anmeldung und der ALTEN X-CLIENT-ID. Antwort {"newXClientId": "…"}.
    Voraussetzung (MediData): die Organisation ist im Webservice schon einem Client zugeordnet und aktiviert."""
    import aiohttp

    if not re.fullmatch(r"[0-9]{1,19}", cloud_instance_id or ""):
        raise MigrationError("cloudclientinstanceid: nur Ziffern (Angabe von MediData bei der Migration)")
    s = settings
    if not (s.medidata_url and s.medidata_client_id and s.medidata_user and s.medidata_password):
        raise MigrationError("Alte Box/VA nicht vollständig eingetragen: NFIZYO_MEDIDATA_URL, _CLIENT_ID (alte), _USER, _PASSWORD")
    try:
        ctx = medidata_cert.client_ssl_context(s) if s.medidata_url.startswith("https://") else None
    except medidata_cert.CertError as exc:
        raise MigrationError(str(exc)) from None
    headers = {**MediDataClient(s).auth_headers(), "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(f"{base_url(s)}/cloudmigration", params={"cloudclientinstanceid": cloud_instance_id},
                                   headers=headers, ssl=ctx if ctx is not None else True) as resp:
                status, body = resp.status, await resp.content.read(100_000)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise MigrationError(f"Box/VA nicht erreichbar ({type(exc).__name__})") from None
    if status != 200:
        raise MigrationError(MIGRATION_ERRORS.get(status, f"MediData antwortet HTTP {status}."))
    data = _json(body)
    new_id = data.get("newXClientId") if isinstance(data, dict) else None
    if not isinstance(new_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", new_id):
        if data is None:
            raise MigrationError("Antwort ist kein JSON")
        raise MigrationError("Antwort enthält keine gültige newXClientId")
    return new_id
