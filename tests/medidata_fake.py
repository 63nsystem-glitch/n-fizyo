"""Test-Hilfe: Wegwerf-PKI und ein gefälschter MediData-Webservice (HTTPS mit gegenseitigem TLS).

Bildet die Schnittstellendokumentation Leistungsabrechnung V5.2.0 nach: participants (limit/offset, Link next), uploads (201,
409 bei gleicher Sendung innerhalb einer Minute, Fehler-JSON {"errors": [{code, …}]}), uploads/{ref}/status(+/detail),
downloads (X-ELA-PENDINGDOCUMENTS), downloads/{ref} (Accept application/octet-stream), PUT downloads/{ref}/status,
notifications (limit/offset, Link next), PUT notifications/{id}/status; dazu CSR-Erneuerung und cloudmigration.
ELA-Pfade gelten unter /md (Box) und /md/srv/id0 (Webservice)."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs7
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CLIENT_ID = "1000026203_1200019144"
NEW_CLIENT_ID = "1000026203_1200019999"
NOW = datetime.now(timezone.utc)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def pem(cert) -> bytes:
    return cert.public_bytes(Encoding.PEM)


def key_pem(key) -> bytes:
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


class PKI:
    def __init__(self, d: Path):
        self.d = d
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        self.ca = (x509.CertificateBuilder().subject_name(_name("N-Fizyo Test-CA")).issuer_name(_name("N-Fizyo Test-CA"))
                   .public_key(self.ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                                key_encipherment=False, data_encipherment=False, key_agreement=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()), critical=False)
                   .sign(self.ca_key, hashes.SHA256()))
        self.ca_path = d / "ca.pem"
        self.ca_path.write_bytes(pem(self.ca))
        server_key = ec.generate_private_key(ec.SECP256R1())
        server = self.issue(server_key.public_key(), "medidata-test", days=30, server=True)
        self.server_cert, self.server_key = d / "server.pem", d / "server.key"
        self.server_cert.write_bytes(pem(server))
        self.server_key.write_bytes(key_pem(server_key))

    def issue(self, public_key, cn: str, *, days: int, server: bool = False):
        start = min(NOW - timedelta(days=1), NOW + timedelta(days=days) - timedelta(days=365))
        b = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "CH"),
                                                               x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
             .issuer_name(self.ca.subject).public_key(public_key).serial_number(x509.random_serial_number())
             .not_valid_before(start).not_valid_after(NOW + timedelta(days=days))
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()), critical=False)
             .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]),
                            critical=False))
        if server:
            b = b.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        return b.sign(self.ca_key, hashes.SHA256())

    def client(self, name: str = "client", *, days: int = 30, cn: str = CLIENT_ID, key=None) -> tuple[Path, Path]:
        """Schlüssel (0600) und Kette (eigenes Zertifikat + CA) für das Client-Zertifikat."""
        key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path, cert_path = self.d / f"{name}.key", self.d / f"{name}.crt"
        key_path.write_bytes(key_pem(key))
        os.chmod(key_path, 0o600)
        cert_path.write_bytes(pem(self.issue(key.public_key(), cn, days=days)) + pem(self.ca))
        os.chmod(cert_path, 0o600)
        return cert_path, key_path


def participant(gln: str, name: str, *, receiver: str | None = None, not_supported=(), tg_allowed=False, laws=(1, 2), costs=False,
                max_receive=20000000) -> dict:
    return {"glnParticipant": gln, "glnReceiver": receiver or gln, "name": name, "street": "Strasse 1", "zipCode": "9999",
            "town": "Ort", "lawTypes": list(laws), "bagNumber": None, "tgTpChange": False, "additionalCosts": costs,
            "maxReceive": max_receive, "notSupported": list(not_supported), "tgAllowed": tg_allowed}


class FakeMediData:
    ROOT = "/{root:md(?:/srv/id0)?}"

    def __init__(self, pki: PKI):
        self.pki = pki
        self.calls: list[dict] = []
        self.renew_mode = "ok"
        self.uploads: list[dict] = []            # jede angenommene Sendung (201)
        self.by_ref: dict[str, dict] = {}
        self.upload_plan: list[str] = []         # ok | timeout | 500 | 408 | drop | err:<CODE>:<status> | noref
        self.participants: list[dict] = []
        self.paging = "link"                     # link | offset | foreign
        self.docs: dict[str, dict] = {}          # Downloads: ref → {meta, content, confirmed}
        self.notes: dict[int, dict] = {}         # Benachrichtigungen: id → {data, confirmed}
        self.force: dict[str, int] = {}          # Endpunkt → HTTP-Code erzwingen (participants, status, downloads, …)
        self.confirm_calls: list[str] = []
        self.delay = 0.0

    # ---------------------------------------------------------------- Server
    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth])
        r = self.ROOT
        app.router.add_get("/", self.root)
        app.router.add_get(r + "/ela/participants", self.get_participants)
        app.router.add_post(r + "/ela/uploads", self.post_upload)
        app.router.add_get(r + "/ela/uploads/{ref}/status", self.get_status)
        app.router.add_get(r + "/ela/uploads/{ref}/status/detail", self.get_status_detail)
        app.router.add_get(r + "/ela/downloads", self.get_downloads)
        app.router.add_get(r + "/ela/downloads/{ref}", self.get_download)
        app.router.add_put(r + "/ela/downloads/{ref}/status", self.put_download_status)
        app.router.add_get(r + "/ela/notifications", self.get_notifications)
        app.router.add_put(r + "/ela/notifications/{nid}/status", self.put_notification_status)
        app.router.add_post("/md/srv/{aid}/client/certificates", self.certificates)
        app.router.add_get("/md/cloudmigration", self.cloudmigration)
        return app

    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.pki.server_cert, self.pki.server_key)
        ctx.verify_mode = ssl.CERT_REQUIRED                    # ohne gültiges Client-Zertifikat keine Verbindung
        ctx.load_verify_locations(self.pki.ca_path)
        return ctx

    def peer_cn(self, request) -> str | None:
        peer = request.transport.get_extra_info("peercert") or {} if request.transport else {}
        for rdn in peer.get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    return v
        return None

    @staticmethod
    def error(status: int, code: str | None = None, message: str = "Fehler") -> web.Response:
        """Fehlerantwort wie Client Schnittstellen V1.5, 3.3."""
        return web.json_response({"errors": [{"userMessageTitle": "An error occurred", "userMessage": message,
                                              "internalMessage": message, "code": code, "supportInfo": None,
                                              "reference": None}]}, status=status)

    @web.middleware
    async def _auth(self, request, handler):
        if "/ela/" in request.path:
            self.calls.append({"method": request.method, "path": request.path, "query": dict(request.query),
                               "cn": self.peer_cn(request), "client_id": request.headers.get("X-CLIENT-ID"),
                               "auth": request.headers.get("Authorization"), "accept": request.headers.get("Accept"),
                               "expect": request.headers.get("Expect")})
            if request.headers.get("X-CLIENT-ID") != CLIENT_ID or not request.headers.get("Authorization", "").startswith("Basic "):
                return self.error(403, None, "Forbidden")
            if self.delay:
                await asyncio.sleep(self.delay)
        return await handler(request)

    def _forced(self, name: str):
        code = self.force.get(name)
        if code:
            return self.error(code, {"downloads": "DOWNLOAD:UNKNOWN-ERROR", "status": "STATUS:UNKNOWN-ERROR",
                                     "participants": "PARTICIPANT:INVALID-PARAMETER-VALUE"}.get(name), "erzwungen")
        return None

    async def root(self, request):
        self.calls.append({"method": "GET", "path": "/", "cn": self.peer_cn(request)})
        return web.Response(text="MediData")

    # ---------------------------------------------------------------- 3.6 Teilnehmerverzeichnis
    def _page(self, request, items: list, default_limit: int, name: str) -> web.Response:
        limit = int(request.query.get("limit", default_limit))
        offset = int(request.query.get("offset", 0))
        page = items[offset:offset + limit]
        headers = {}
        if offset + limit < len(items):
            nxt = f"{request.path}?limit={limit}&offset={offset + limit}"
            if self.paging == "link":
                headers["Link"] = f'<{request.url.origin()}{nxt}>; rel="next"'
            elif self.paging == "foreign":                    # Link auf fremden Server: darf nicht befolgt werden
                headers["Link"] = f'<https://evil.invalid{nxt}>; rel="next"'
        return web.json_response(page, headers=headers)

    async def get_participants(self, request):
        return self._forced("participants") or self._page(request, self.participants, 50, "participants")

    # ---------------------------------------------------------------- 3.7 Versand
    async def post_upload(self, request):
        reader = await request.multipart()
        parts = {}
        order = []
        async for part in reader:
            order.append(part.name)
            parts[part.name] = (await part.read(), part.headers.get("Content-Type"), part.filename)
        plan = self.upload_plan.pop(0) if self.upload_plan else "ok"
        if plan.startswith("err:"):                               # err:<CODE>:<HTTP-Status>
            bits = plan.split(":")
            return self.error(int(bits[-1]), ":".join(bits[1:-1]) or None, "abgelehnt")
        if plan == "408":
            return self.error(408, None, "Request Timeout")
        if "elauploadstream" not in parts:
            return self.error(400, "UPLOAD:NO-RECEIVER-DEFINED", "kein Dokument")
        xml = parts["elauploadstream"][0]
        info_raw = parts.get("elauploadinfo", (b"{}",))[0]
        info = json.loads(info_raw or b"{}")
        key = hashlib.sha256(xml + info_raw).hexdigest()
        now = datetime.now(timezone.utc)
        for up in self.uploads:                                # 3.7.5: gleiche Sendung innerhalb einer Minute → 409
            if up["key"] == key and now - up["at"] < timedelta(minutes=1):
                return web.json_response({"transmissionReference": up["ref"]}, status=409)
        if plan == "500":
            return self.error(500, "UPLOAD:UNKNOWN-ERROR", "Serverfehler")
        ref = str(uuid.uuid4())
        rec = {"ref": ref, "xml": xml, "info": info, "key": key, "at": now, "order": order, "cn": self.peer_cn(request),
               "parts": {k: (v[1], v[2]) for k, v in parts.items()}, "status": "PROCESSING", "errorReason": None,
               "receivers": [{"status": "PROCESSING", "receiverType": "MAIN_RECEIVER", "receiverDisplayName": "Versicherung",
                              "fetchedDate": None}]}
        self.uploads.append(rec)
        self.by_ref[ref] = rec
        if plan == "timeout":                                  # angekommen, aber die Antwort kommt zu spät
            await asyncio.sleep(1.5)
        if plan == "drop":                                     # angekommen, Verbindung bricht vor der Antwort ab
            request.transport.close()
            await asyncio.sleep(0.2)
        if plan == "noref":
            return web.json_response({}, status=201)
        return web.json_response({"transmissionReference": ref}, status=201)

    # ---------------------------------------------------------------- 3.8 Status
    async def get_status(self, request):
        forced = self._forced("status")
        if forced:
            return forced
        rec = self.by_ref.get(request.match_info["ref"])
        if rec is None:
            return self.error(404, "UPLOAD:REFERENCE-NOT-FOUND", "nicht gefunden")
        return web.json_response({"transmissionReference": rec["ref"], "created": "2026-11-08T12:00:33.000+0100",
                                  "modified": "2026-11-08T12:05:00.000+0100", "status": rec["status"], "errorReason": rec["errorReason"]})

    async def get_status_detail(self, request):
        rec = self.by_ref.get(request.match_info["ref"])
        if rec is None:
            return self.error(404, "UPLOAD:REFERENCE-NOT-FOUND", "nicht gefunden")
        return web.json_response({"transmissionReference": rec["ref"], "created": "2026-11-08T12:00:33.000+0100",
                                  "modified": "2026-11-08T12:05:00.000+0100", "status": rec["status"], "errorReason": rec["errorReason"],
                                  "receivers": rec["receivers"]})

    # ---------------------------------------------------------------- 3.9 Dokumente
    def add_doc(self, content: bytes, *, doc_type="application/x-fd-geninv-resp-v50+xml", correlation="", modus="TEST",
                sender="2099988876514") -> str:
        ref = str(uuid.uuid4())
        self.docs[ref] = {"meta": {"transmissionReference": ref, "documentReference": f"doc-{len(self.docs) + 1}",
                                   "correlationReference": correlation, "senderGln": sender, "docType": doc_type,
                                   "fileSize": len(content), "modus": modus, "status": "PENDING",
                                   "created": "2026-11-08T12:00:33.000+0100"},
                          "content": content, "confirmed": 0, "fetched": 0}
        return ref

    async def get_downloads(self, request):
        forced = self._forced("downloads")
        if forced:
            return forced
        limit = int(request.query.get("limit", 10))
        pending = [d for d in self.docs.values() if not d["confirmed"]]
        page = pending[:limit]
        return web.json_response([d["meta"] for d in page], headers={"X-ELA-PENDINGDOCUMENTS": str(len(pending) - len(page))})

    async def get_download(self, request):
        if request.headers.get("Accept") != "application/octet-stream":
            return self.error(406, None, "Not Acceptable")
        doc = self.docs.get(request.match_info["ref"])
        if doc is None:
            return self.error(404, "DOWNLOAD:REFERENCE-NOT-FOUND", "nicht gefunden")
        doc["fetched"] += 1
        return web.Response(body=doc["content"], content_type="application/octet-stream")

    async def put_download_status(self, request):
        ref = request.match_info["ref"]
        self.confirm_calls.append(ref)
        body = await request.json()
        if body != {"status": "CONFIRMED"} or request.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.error(400, "DOWNLOAD:INVALID-PARAMETER-VALUE", "falscher Status")
        doc = self.docs.get(ref)
        if doc is None:
            return self.error(404, "DOWNLOAD:REFERENCE-NOT-FOUND", "nicht gefunden")
        if self.force.get("confirm"):
            return self.error(self.force["confirm"], "CONFIRMATION:UNKNOWN-ERROR", "erzwungen")
        doc["confirmed"] += 1
        return web.json_response({**doc["meta"], "status": "CONFIRMED"})

    # ---------------------------------------------------------------- 3.10 Benachrichtigungen
    def add_note(self, nid: int, *, severity="ERROR", code="UPLOAD:NO-RECEIVER-DEFINED", tref=None, dref=None, cref=None, mode="TEST"):
        self.notes[nid] = {"data": {
            "id": nid, "subject": {"de": "Übermittlung fehlgeschlagen", "fr": "La transmission a échoué", "it": "Trasmissione fallita"},
            "message": {"de": f"Fehler-Code: {code}", "fr": f"Code d'erreur: {code}", "it": f"Codice: {code}"},
            "severity": severity, "read": False, "created": "2026-11-08T09:11:53.289+0200", "template": "elaUploadFailed",
            "mode": mode, "notificationParameters": {"correlationReference": cref, "transmissionReference": tref, "code": code,
                                                      "documentReference": dref},
            "errorCode": code, "correlationReference": cref, "transmissionReference": tref, "documentReference": dref,
            "potentialReasons": {"de": "Kein Empfänger", "fr": "Aucun destinataire"},
            "possibleSolutions": {"de": "Empfänger angeben", "fr": "Indiquer un destinataire"},
            "technicalInformation": "ValidationException"}, "confirmed": 0}

    async def get_notifications(self, request):
        forced = self._forced("notifications")
        if forced:
            return forced
        items = [n["data"] for n in self.notes.values() if not n["confirmed"]]
        return self._page(request, items, 50, "notifications")

    async def put_notification_status(self, request):
        body = await request.json()
        if body != {"notificationFetched": True}:
            return self.error(400, None, "falscher Inhalt")
        note = self.notes.get(int(request.match_info["nid"]))
        if note is None:
            return self.error(404, None, "nicht gefunden")
        note["confirmed"] += 1
        return web.Response(status=200)

    # ---------------------------------------------------------------- Zertifikat und Migration
    async def certificates(self, request):
        body = await request.read()
        self.calls.append({"path": request.path, "cn": self.peer_cn(request), "accept": request.headers.get("Accept"),
                           "ctype": request.headers.get("Content-Type"), "auth": request.headers.get("Authorization")})
        if self.renew_mode == "raw400":
            return web.json_response({"message": "kaputt"}, status=400)
        if self.renew_mode != "ok":
            return web.json_response({"errorCode": f"CLIENT-CERT-RENEWAL:{self.renew_mode}", "message": "…"}, status=400)
        csr = x509.load_pem_x509_csr(body)
        cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        cert = self.pki.issue(csr.public_key(), cn, days=3 * 365)
        if request.headers.get("Accept") == "application/json":
            return web.json_response({"serialNumber": f"{cert.serial_number:X}", "validFrom": cert.not_valid_before_utc.isoformat(),
                                      "validTo": cert.not_valid_after_utc.isoformat(),
                                      "pkcs7": pkcs7.serialize_certificates([cert, self.pki.ca], Encoding.PEM).decode()})
        return web.Response(body=pem(self.pki.ca) + pem(cert), content_type="application/pem-certificate-chain")

    async def cloudmigration(self, request):
        self.calls.append({"path": request.path, "query": dict(request.query), "headers": dict(request.headers)})
        if request.headers.get("X-CLIENT-ID") != CLIENT_ID or "Basic " not in request.headers.get("Authorization", ""):
            return web.Response(status=403)
        if not request.query.get("cloudclientinstanceid"):
            return web.Response(status=400)
        return web.json_response({"newXClientId": NEW_CLIENT_ID})
