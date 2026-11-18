"""MediData-Webservice: Client-Zertifikat (gegenseitiges TLS) – Ablauf überwachen, CSR erstellen, Zertifikat erneuern.

Quelle: MediData «Interface description Requesting a client certificate with CSR», V 1.0 (2023):
- Pfad md/srv/<applianceid>/client/, Endpunkt POST …/certificates (Beispiel https://services.medidata.ch/md/srv/id3/client/certificates).
- Body: CSR im PEM-Format, Content-Type application/octet-stream. Header Accept (Pflicht):
  application/pem-certificate-chain (Kette als PEM) · application/json (serialNumber, validFrom, validTo, pkcs7) ·
  application/pkcs7-mime (hier nicht genutzt).
- CSR (Anhang 5.2): RSA 2048; C = CH, L = Ort, O = Firma, CN = Client-ID (X-Client-ID).
- Neues Zertifikat ab 60 Tage vor Ablauf des gültigen, mit dem gleichen oder einem neuen CSR. Höchstens zwei aktive Zertifikate
  pro Client (auch die aus dem Kundenportal). Wurde für den CSR schon eines ausgestellt, kommt dieses zurück (gleicher CSR = sicher
  wiederholbar).
- Fehler 400 mit Code im JSON (Anhang 5.1): CLIENT-CERT-RENEWAL:CSR-INVALID | CN-INVALID | NEW-CSR-OUTSIDE-RENEWAL-PERIOD |
  TOO-MANY-ACTIVE-CERTIFICATES | UNKNOWN-ERROR. MediData: die Codes können sich ändern → unbekannte werden roh gezeigt.
  Der Name des JSON-Felds steht nicht im Dokument → der Code wird im ganzen Antworttext gesucht.
- Nur Accept/Content-Type/Body sind beschrieben – keine weiteren Header (Anmeldung über das Client-Zertifikat).

Sicherheit: Die TLS-Prüfung wird nie abgeschaltet. Für das selbstsignierte Zertifikat einer Testumgebung (Simulator-Beschreibung
2.5, «Known Bugs» 3.2) wird nur einer ausdrücklich angegebenen CA-Datei zusätzlich vertraut (NFIZYO_MEDIDATA_CA).
Der private Schlüssel muss «nur Besitzer» sein (chmod 600/400) – sonst wird er nicht verwendet. Besitzer ist der Dienst-Benutzer
(nfizyo), der ihn für das Senden lesen muss. Die SSL-Einstellungen werden bei jeder Anfrage neu gelesen (kein Neustart nach Erneuerung).
"""
from __future__ import annotations

import json
import os
import re
import ssl
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

RENEWAL_DAYS = 60          # Erneuerung ab 60 Tage vor Ablauf (MediData)
MAX_ACTIVE = 2             # höchstens zwei aktive Zertifikate pro Client
ACCEPT = {"pem": "application/pem-certificate-chain", "json": "application/json"}

# Fehlercodes laut Anhang 5.1 → Klartext (Kommandozeile für NSystem, deutsch wie die übrigen Befehle)
ERRORS = {
    "CSR-INVALID": "Der CSR ist leer oder ungültig. Mit «medidata-csr» neu erstellen.",
    "CN-INVALID": "Der Common Name (CN) im CSR ist leer oder ungültig. Er muss genau die X-Client-ID sein.",
    "NEW-CSR-OUTSIDE-RENEWAL-PERIOD": "Ein neuer CSR ist erst ab 60 Tagen vor Ablauf des gültigen Zertifikats erlaubt. "
                                      "Bis dahin warten oder den bisherigen CSR verwenden.",
    "TOO-MANY-ACTIVE-CERTIFICATES": "Es gibt schon zwei aktive Zertifikate (auch die aus dem Kundenportal zählen). "
                                    "Erst wenn eines abgelaufen oder im Kundenportal widerrufen ist, geht ein drittes.",
    "UNKNOWN-ERROR": "Unbekannter Fehler bei MediData. Später nochmals versuchen, sonst den MediData-Support fragen.",
}
CODE_RX = re.compile(r"CLIENT-CERT-RENEWAL:([A-Z0-9-]+)")
CLIENT_ID_RX = re.compile(r"[A-Za-z0-9_.-]{1,64}")
APPLIANCE_RX = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class CertError(Exception):
    """Zertifikat, Schlüssel oder Einstellung nicht brauchbar – es wurde nichts gesendet."""


class RenewError(Exception):
    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- Dateien prüfen
def _stat(path: str, what: str) -> os.stat_result:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise CertError(f"{what} fehlt: {path}") from None
    except OSError as exc:
        raise CertError(f"{what} nicht lesbar: {path} ({exc.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        raise CertError(f"{what} ist keine Datei: {path}")
    return st


def check_key_file(path: str) -> None:
    """Privater Schlüssel: nur für den Besitzer (0600 oder 0400). Gruppe/andere dürfen nichts."""
    mode = stat.S_IMODE(_stat(path, "Schlüsseldatei").st_mode)
    if mode & 0o077:
        raise CertError(f"Schlüsseldatei {path} ist für Gruppe/andere zugänglich (Rechte {mode:04o}) und wird nicht verwendet. "
                        f"Nur der Besitzer darf sie lesen: chmod 600 {path}")


def check_public_file(path: str, what: str) -> None:
    """Zertifikat / CA-Datei: lesbar darf sie sein, aber niemand ausser dem Besitzer darf sie ändern."""
    mode = stat.S_IMODE(_stat(path, what).st_mode)
    if mode & 0o022:
        raise CertError(f"{what} {path} ist für Gruppe/andere beschreibbar (Rechte {mode:04o}): chmod 600 {path}")


def _read(path: str, what: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise CertError(f"{what} nicht lesbar: {path} ({exc.strerror}) – Besitzer muss der Dienst-Benutzer sein") from None


def load_chain(path: str) -> list:
    from cryptography import x509

    check_public_file(path, "Zertifikatsdatei")
    try:
        return x509.load_pem_x509_certificates(_read(path, "Zertifikatsdatei"))
    except ValueError:
        raise CertError(f"Zertifikatsdatei {path} enthält kein Zertifikat im PEM-Format") from None


def load_key(path: str):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    check_key_file(path)
    data = _read(path, "Schlüsseldatei")
    try:
        return load_pem_private_key(data, password=None)
    except TypeError:
        raise CertError(f"Schlüssel {path} ist mit Passwort geschützt. N-Fizyo braucht ihn ohne Passwort "
                        "(dafür nur für den Dienst lesbar).") from None
    except Exception:  # noqa: BLE001 - beliebige Parser-Fehler: kein brauchbarer Schlüssel
        raise CertError(f"Schlüsseldatei {path} enthält keinen privaten Schlüssel im PEM-Format") from None


def spki(public_key) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def common_name(name) -> str | None:
    from cryptography.x509.oid import NameOID

    attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else None


# --------------------------------------------------------------------------- TLS
def configured(settings) -> bool:
    return bool(settings.medidata_cert or settings.medidata_key)


def _no_password(*_a) -> bytes:
    raise CertError("Schlüssel ist mit Passwort geschützt – N-Fizyo braucht ihn ohne Passwort.")


def client_ssl_context(settings) -> ssl.SSLContext | None:
    """SSL-Einstellungen für jede Anfrage an MediData. None = ohne Client-Zertifikat und ohne eigene CA (normale Prüfung).

    Die Serverprüfung bleibt immer an (Zertifikat + Name). NFIZYO_MEDIDATA_CA wird den Systemzertifikaten hinzugefügt."""
    cert, key, ca = settings.medidata_cert, settings.medidata_key, settings.medidata_ca
    if not (cert or key or ca):
        return None
    if bool(cert) != bool(key):
        raise CertError("NFIZYO_MEDIDATA_CERT und NFIZYO_MEDIDATA_KEY gehören zusammen – beide eintragen oder beide leer lassen.")
    ctx = ssl.create_default_context()
    if ca:
        check_public_file(ca, "CA-Datei")
        try:
            ctx.load_verify_locations(cafile=ca)
        except (OSError, ssl.SSLError):
            raise CertError(f"CA-Datei {ca} ist nicht lesbar oder kein Zertifikat im PEM-Format") from None
    if cert:
        check_public_file(cert, "Zertifikatsdatei")
        load_key(key)                                  # Rechte, Passwort, Format – klare Meldung vor OpenSSL
        try:
            ctx.load_cert_chain(cert, key, password=_no_password)
        except ssl.SSLError:
            raise CertError("Zertifikat und Schlüssel passen nicht zusammen (oder die Zertifikatsdatei ist kein PEM).") from None
        except OSError as exc:
            raise CertError(f"Datei nicht lesbar: {exc.filename or cert}") from None
    return ctx


# --------------------------------------------------------------------------- Ablauf überwachen
@dataclass
class CertStatus:
    state: str                      # off | ok | warn | expired | error
    message: str = ""
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    days_left: int | None = None
    serial: str | None = None
    cn_matches: bool | None = None  # CN == NFIZYO_MEDIDATA_CLIENT_ID (None = keine Client-ID eingetragen)

    @property
    def renew_from(self) -> datetime | None:
        return self.valid_to - timedelta(days=RENEWAL_DAYS) if self.valid_to else None


def days_until(end: datetime, now: datetime) -> int:
    return int((end - now).total_seconds() // 86400)


def status(settings, now: datetime | None = None) -> CertStatus:
    """Gültigkeit des Client-Zertifikats. warn = höchstens 60 Tage (Erneuerung möglich), expired/error = so nicht nutzbar."""
    cert, key = settings.medidata_cert, settings.medidata_key
    if not (cert or key):
        return CertStatus("off", "kein Client-Zertifikat eingetragen (NFIZYO_MEDIDATA_CERT/_KEY) – nur für den Webservice nötig")
    if not (cert and key):
        return CertStatus("error", "NFIZYO_MEDIDATA_CERT und NFIZYO_MEDIDATA_KEY gehören zusammen – beide eintragen")
    try:
        chain = load_chain(cert)
        private = load_key(key)
    except CertError as exc:
        return CertStatus("error", str(exc))
    leaf = chain[0]
    now = now or datetime.now(timezone.utc)
    end, start = leaf.not_valid_after_utc, leaf.not_valid_before_utc
    cn = common_name(leaf.subject)
    out = CertStatus("ok", valid_from=start, valid_to=end, days_left=days_until(end, now), serial=f"{leaf.serial_number:X}",
                     cn_matches=(cn == settings.medidata_client_id) if settings.medidata_client_id else None)
    if spki(private.public_key()) != spki(leaf.public_key()):
        out.state, out.message = "error", "Schlüssel passt nicht zum Zertifikat (erstes Zertifikat der Kette)"
    elif end <= now:
        out.state, out.message = "expired", f"abgelaufen am {end:%d.%m.%Y}"
    elif start > now:
        out.state, out.message = "warn", f"erst gültig ab {start:%d.%m.%Y}"
    elif out.days_left <= RENEWAL_DAYS:
        out.state, out.message = "warn", f"läuft in {out.days_left} Tagen ab ({end:%d.%m.%Y}) – erneuern: python -m nfizyo medidata-renew"
    else:
        out.message = f"gültig bis {end:%d.%m.%Y} ({out.days_left} Tage)"
    if out.cn_matches is False and out.state in ("ok", "warn"):
        out.state = "warn"
        out.message += " · CN des Zertifikats ist nicht NFIZYO_MEDIDATA_CLIENT_ID"
    return out


def summary(settings) -> dict | None:
    """Für die NSystem-Übersicht: nur Zustand und Tage – keine Pfade, keine Client-ID, keine Seriennummer."""
    st = status(settings)
    if st.state == "off":
        return None
    return {"state": st.state, "days_left": st.days_left}


# --------------------------------------------------------------------------- Schlüssel und CSR
def new_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def write_private_key(path: Path, key) -> None:
    """Neue Schlüsseldatei mit Rechten 0600. Überschreibt nie eine vorhandene Datei."""
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    data = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def new_key_path(key_path: Path) -> Path:
    """Schlüssel für einen neuen CSR, solange das alte Zertifikat noch gilt: client.key → client.new.key"""
    return key_path.with_name(f"{key_path.stem}.new{key_path.suffix or '.key'}")


def csr_path_for(key_path: Path) -> Path:
    return key_path.with_suffix(".csr")


def _clean(value: str, what: str, limit: int = 64) -> str:
    v = (value or "").strip()
    if not v or len(v) > limit or any(ord(ch) < 32 for ch in v):
        raise CertError(f"{what} fehlt oder ist ungültig (1–{limit} Zeichen)")
    return v


def validate_subject(*, client_id: str, org: str, city: str, country: str = "CH") -> tuple[str, str, str, str]:
    """(C, L, O, CN) geprüft – vor dem Erzeugen eines Schlüssels aufrufen."""
    if not CLIENT_ID_RX.fullmatch(client_id or ""):
        raise CertError("Client-ID (CN) fehlt oder ist ungültig – sie steht in NFIZYO_MEDIDATA_CLIENT_ID (Beispiel 1234567890_1234567890)")
    if not re.fullmatch(r"[A-Z]{2}", country or ""):
        raise CertError("Land (C) muss ein Ländercode mit zwei Buchstaben sein, z. B. CH")
    return country, _clean(city, "Ort (L)"), _clean(org, "Firma (O)"), client_id


def build_csr(key, *, client_id: str, org: str, city: str, country: str = "CH") -> bytes:
    """CSR wie im MediData-Beispiel (csr.conf): C, L, O, CN = Client-ID. SHA-256."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    c, loc, o, cn = validate_subject(client_id=client_id, org=org, city=city, country=country)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, c),
        x509.NameAttribute(NameOID.LOCALITY_NAME, loc),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, o),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])
    csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(key, hashes.SHA256())
    return csr.public_bytes(Encoding.PEM)


def load_csr(path: Path):
    from cryptography import x509

    try:
        csr = x509.load_pem_x509_csr(path.read_bytes())
    except FileNotFoundError:
        raise CertError(f"CSR fehlt: {path} – zuerst «python -m nfizyo medidata-csr» ausführen") from None
    except (OSError, ValueError):
        raise CertError(f"{path} ist kein CSR im PEM-Format") from None
    if not csr.is_signature_valid:
        raise CertError(f"CSR {path} ist beschädigt (Signatur ungültig)")
    return csr


def write_public_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Atomar schreiben (temporäre Datei im selben Ordner, dann umbenennen)."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Erneuern
def endpoint(settings) -> str:
    """…/md/srv/<applianceid>/client/certificates. Ohne eigene Angaben aus NFIZYO_MEDIDATA_URL des Webservice
    (https://services[-acc].medidata.ch/md/srv/id0) abgeleitet."""
    base, aid = settings.medidata_service_url, settings.medidata_appliance_id
    m = re.fullmatch(r"(https://[^/]+)/md/srv/([A-Za-z0-9_.-]{1,64})", (getattr(settings, "medidata_url", "") or "").rstrip("/"))
    if not aid and m:
        base, aid = m.group(1), m.group(2)
    if not base.startswith("https://"):
        raise CertError("NFIZYO_MEDIDATA_SERVICE_URL muss mit https:// beginnen")
    if not aid:
        raise CertError("NFIZYO_MEDIDATA_APPLIANCE_ID fehlt (Pfad md/srv/<applianceid>/client/ – Angabe von MediData)")
    if not APPLIANCE_RX.fullmatch(aid):
        raise CertError("NFIZYO_MEDIDATA_APPLIANCE_ID ist ungültig (nur Buchstaben, Ziffern, _ . -)")
    return f"{base}/md/srv/{aid}/client/certificates"


@dataclass
class Issued:
    chain: list                     # x509.Certificate, eigenes Zertifikat zuerst (nach install geordnet)
    serial: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


def parse_issued(body: bytes, accept: str) -> Issued:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs7

    try:
        if accept == "json":
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("pkcs7"), str):
                raise ValueError("pkcs7 fehlt")
            chain = pkcs7.load_pem_pkcs7_certificates(data["pkcs7"].encode("ascii"))
            return Issued(list(chain), serial=str(data.get("serialNumber") or "") or None,
                          valid_from=str(data.get("validFrom") or "") or None, valid_to=str(data.get("validTo") or "") or None)
        return Issued(x509.load_pem_x509_certificates(body))
    except (ValueError, UnicodeDecodeError):
        raise RenewError("Die Antwort von MediData enthält kein lesbares Zertifikat – nichts gespeichert.", status=200) from None


async def request_certificate(settings, csr_pem: bytes, *, accept: str = "pem", timeout: float = 60) -> Issued:
    """POST des CSR mit dem aktuellen Client-Zertifikat. Wirft CertError (nichts gesendet) oder RenewError."""
    import asyncio

    import aiohttp

    url = endpoint(settings)
    if not (settings.medidata_cert and settings.medidata_key):
        raise CertError("Für die Erneuerung braucht es das aktuelle Client-Zertifikat (NFIZYO_MEDIDATA_CERT und _KEY).")
    ctx = client_ssl_context(settings)
    headers = {"Accept": ACCEPT[accept], "Content-Type": "application/octet-stream"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url, data=csr_pem, headers=headers, ssl=ctx) as resp:
                body = await resp.content.read(1_000_000)
                code = resp.status
    except aiohttp.ClientConnectorError as exc:
        raise RenewError(f"MediData nicht erreichbar ({type(exc).__name__}) – nichts gesendet.") from None
    except asyncio.TimeoutError:
        raise RenewError("Zeitüberschreitung – Stand unklar. Später nochmals mit dem gleichen CSR versuchen "
                         "(MediData liefert dann das schon ausgestellte Zertifikat).") from None
    except aiohttp.ClientError as exc:
        raise RenewError(f"Verbindung abgebrochen ({type(exc).__name__}) – wird das Client-Zertifikat abgelehnt? "
                         "Nichts gespeichert.") from None
    if code == 200:
        return parse_issued(body, accept)
    text = body.decode("utf-8", "replace")
    if code == 400:
        found = CODE_RX.search(text)
        short = found.group(1) if found else None
        if short in ERRORS:
            raise RenewError(f"{ERRORS[short]} (CLIENT-CERT-RENEWAL:{short})", code=short, status=400)
        if short:
            raise RenewError(f"MediData lehnt ab: CLIENT-CERT-RENEWAL:{short}", code=short, status=400)
        raise RenewError(f"MediData lehnt die Anfrage ab (400): {text[:200]}", status=400)
    if code in (401, 403):
        raise RenewError(f"Zugang abgelehnt (HTTP {code}) – gültiges Client-Zertifikat dieses Clients nötig.", status=code)
    raise RenewError(f"MediData antwortet HTTP {code} – nichts gespeichert.", status=code)


def install(settings, issued: Issued, csr, *, new_key: Path | None = None, now: datetime | None = None) -> dict:
    """Neue Kette speichern: eigenes Zertifikat zuerst, passend zum CSR. Alte Kette (und bei neuem Schlüssel der alte Schlüssel)
    bleiben als Sicherung «….bak-JJJJMMTT-HHMMSS». Ersetzen per Umbenennen (atomar)."""
    from cryptography.hazmat.primitives.serialization import Encoding

    want = spki(csr.public_key())
    leaf = next((c for c in issued.chain if spki(c.public_key()) == want), None)
    if leaf is None:
        raise RenewError("Das erhaltene Zertifikat passt nicht zum CSR – nichts gespeichert.")
    ordered = [leaf] + [c for c in issued.chain if c is not leaf]
    issued.chain = ordered
    cert_path, key_path = Path(settings.medidata_cert), Path(settings.medidata_key)
    if new_key is not None:
        if spki(load_key(str(new_key)).public_key()) != want:
            raise RenewError("Neuer Schlüssel passt nicht zum CSR – nichts gespeichert.")
    elif spki(load_key(str(key_path)).public_key()) != want:
        raise RenewError("CSR gehört nicht zum eingetragenen Schlüssel – nichts gespeichert.")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    backups = {}
    if cert_path.exists():
        backups["cert"] = cert_path.with_name(f"{cert_path.name}.bak-{stamp}")
        _copy_private(cert_path, backups["cert"])
    pem = b"".join(c.public_bytes(Encoding.PEM) for c in ordered)
    tmp = cert_path.with_name(f".{cert_path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, pem)
        os.fsync(fd)
    finally:
        os.close(fd)
    if new_key is not None:
        backups["key"] = key_path.with_name(f"{key_path.name}.bak-{stamp}")
        _copy_private(key_path, backups["key"])
        os.replace(new_key, key_path)
    os.replace(tmp, cert_path)
    return {"backups": backups, "leaf": leaf}


def _copy_private(src: Path, dst: Path) -> None:
    data = src.read_bytes()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)
