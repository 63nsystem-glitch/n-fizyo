"""SMS-Versand über den Postausgang (Tabelle outbox).

- NFIZYO_SMS=log (Standard, Demo, Tests): nichts verlässt die Box, die SMS steht nur im Postausgang.
- NFIZYO_SMS=live: Versand über den Anbieter, den die Praxis unter Einstellungen › SMS gewählt hat:
    ASPSMS (VADIAN.NET AG, St. Gallen)  – Zugangsdaten NFIZYO_ASPSMS_USERKEY / NFIZYO_ASPSMS_PASSWORD
    eCall  (F24 Schweiz AG)             – Zugangsdaten NFIZYO_ECALL_USERNAME / NFIZYO_ECALL_PASSWORD
  Zugangsdaten stehen NUR in der .env-Datei der Box (nie in der Datenbank, nie in Logs, nie im Chat).

Jede SMS wird zuerst im Postausgang gespeichert und dann sofort im Hintergrund versendet (eigener Ablauf, nicht in der
KI-Warteschlange – ein Anmeldecode darf nicht hinter einer Transkription warten). Fehlgeschlagene Versuche werden bis
zu 5-mal wiederholt (nach 1, 5, 15, 60 Minuten). Vor dem Echtbetrieb: Auftragsverarbeitungsvertrag mit dem Anbieter.

Geheimnisse (Anmeldecodes, Einladungs-, Video- und Angebotslinks) gibt der Aufrufer mit send(…, secrets=[…]) an:
outbox.body enthält dann nur die maskierte Fassung («••••••»), der volle Text steht in outbox.body_secret und wird nach dem
Versand (oder dem endgültigen Fehlschlag) gelöscht. Im Testmodus (log) ohne Demo wird gar kein Klartext gespeichert; nur die
Demo zeigt den vollen Text (fiktive Daten, nichts wird versendet). body_secret ist nie im Datenexport.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow

log = logging.getLogger(__name__)

ASPSMS_URL = "https://json.aspsms.com/SendSimpleTextSMS"   # JSON-Schnittstelle (json.aspsms.com)
ECALL_URL = "https://rest.ecall.ch/api/v2/message"          # REST-Schnittstelle (developer.ecall-messaging.com)
PROVIDERS = ("log", "aspsms", "ecall")
RETRY_MINUTES = (1, 5, 15, 60)
MAX_ATTEMPTS = len(RETRY_MINUTES) + 1
MAX_LEN = 459  # höchstens 3 SMS-Teile


class SmsError(Exception):
    """Versand nicht möglich (Text ohne Zugangsdaten / Antwort des Anbieters)."""


def normalize_phone(raw: str | None) -> str | None:
    """Schweizer und internationale Nummern → +41791234567. Ungültig → None.

    Ausland nur, wenn die Nummer ausdrücklich mit + oder 00 beginnt. Schweizer Nummern ohne führende 0 (Excel lässt sie weg,
    oder getippt «79 123 45 67») werden zu +41…; «41791234567» (+ vergessen) zu +41…. Andere Ziffernfolgen ohne 0/00/+
    werden NICHT geraten (früher «791234567» → +791234567 = Russland/Kasachstan). +41/+423 nur mit der richtigen Länge."""
    return classify_phone(raw)[0]


def classify_phone(raw: str | None) -> tuple[str | None, str]:
    """(Nummer +41…, Art): ok | guessed (ohne 0/+ eingegeben, als Schweizer Nummer gelesen) | foreign (ausdrücklich Ausland)
    | invalid (unmöglich – z. B. falsche Länge oder Ziffern ohne Vorwahl) | empty."""
    if raw is None or not str(raw).strip():
        return None, "empty"
    s = str(raw).strip().replace("(0)", "")                   # «+41 (0)79 …»
    s = re.sub(r"[\s\-/().   '’]", "", s)
    how = "ok"
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0"):
        s = "+41" + s[1:]
    elif s.startswith("+"):
        pass
    elif re.fullmatch(r"[1-9]\d{8}", s):                     # 9 Ziffern: Schweizer Nummer ohne führende 0
        s, how = "+41" + s, "guessed"
    elif re.fullmatch(r"(?:41[1-9]\d{8}|423\d{7})", s):      # Landesvorwahl CH/FL ohne +
        s, how = "+" + s, "guessed"
    else:
        return None, "invalid"
    if s.startswith("+410"):                                 # +41 079 … → +41 79 …
        s = "+41" + s[4:]
    if not re.fullmatch(r"\+[1-9]\d{7,14}", s):
        return None, "invalid"
    if s.startswith("+41"):
        return (s, how) if re.fullmatch(r"\+41[1-9]\d{8}", s) else (None, "invalid")
    if s.startswith("+423"):
        return (s, how) if re.fullmatch(r"\+423\d{7}", s) else (None, "invalid")
    return s, "foreign"


def ip_bucket(ip: str | None) -> str:
    """Schlüssel für Bremsen pro Adresse: IPv4 wie sie ist, IPv6 pro /64-Netz (jeder Anschluss hat ein ganzes /64 – sonst
    umgeht man jede Bremse mit wechselnden Adressen)."""
    import ipaddress

    try:
        addr = ipaddress.ip_address(str(ip or "").strip())
    except ValueError:
        return str(ip or "unbekannt")
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


# Obergrenze für SMS, die jemand OHNE Anmeldung auslösen kann (Codes der Online-Termine und der App-Anmeldung) – pro Praxis,
# über alle Adressen und Nummern. Schützt vor Kosten und vor SMS im Namen der Praxis an fremde Leute.
ANON_SMS_PER_HOUR = 30
ANON_SMS_PER_DAY = 100


def anon_sms_blocked(throttle, practice_id: int) -> bool:
    """Ist die Obergrenze der Praxis für anonym ausgelöste SMS erreicht (Stunde oder Tag)? Einmal pro Stunde ins Protokoll."""
    hour = throttle.blocked(f"anon-sms-h:{practice_id}", ANON_SMS_PER_HOUR, 3600)
    day = throttle.blocked(f"anon-sms-d:{practice_id}", ANON_SMS_PER_DAY, 86400)
    if (hour or day) and not throttle.blocked(f"anon-sms-warned:{practice_id}", 1, 3600):
        throttle.hit(f"anon-sms-warned:{practice_id}", 3600)
        log.warning("Praxis %s: Obergrenze für SMS ohne Anmeldung erreicht (%s) – weitere Codes werden verweigert",
                    practice_id, "Stunde" if hour else "Tag")
    return hour or day


def anon_sms_hit(throttle, practice_id: int) -> None:
    throttle.hit(f"anon-sms-h:{practice_id}", 3600)
    throttle.hit(f"anon-sms-d:{practice_id}", 86400)


# Mobilnummern Schweiz (075–079, neu auch 074) und Liechtenstein (6xx/7xx, 7-stellig)
_MOBILE_CH_LI = re.compile(r"\+41(7[4-9])\d{7}|\+423[67]\d{6}")


def is_ch_li_mobile(number: str | None) -> bool:
    """Nur Schweizer und Liechtensteiner Handynummern (öffentliche Formulare: keine SMS ins Ausland auf Kosten der Praxis)."""
    return bool(number) and _MOBILE_CH_LI.fullmatch(number) is not None


def is_landline(number: str | None) -> bool:
    """Schweizer/Liechtensteiner Festnetznummer: SMS kommt nicht an (oder wird teuer vorgelesen). Ausland: unbekannt → False."""
    n = normalize_phone(number)
    return bool(n) and (n.startswith("+41") or n.startswith("+423")) and not is_ch_li_mobile(n)


SECRET_MASK = "••••••"


def mask_secrets(text: str, secrets=()) -> str:
    """Codes/Tokens im Text unkenntlich machen (längere zuerst, damit ein Code in einem Link nicht stört)."""
    for secret in sorted({str(x) for x in secrets if x and len(str(x)) >= 4}, key=len, reverse=True):
        text = text.replace(secret, SECRET_MASK)
    return text


def mask(recipient: str) -> str:
    return (recipient[:5] + "…" + recipient[-2:]) if len(recipient) > 8 else "…"


def clean_sender(value: str | None) -> str:
    """Absendername: max. 11 Zeichen, nur Buchstaben und Ziffern (Vorgabe der Mobilfunknetze)."""
    s = re.sub(r"[^A-Za-z0-9]", "", value or "")
    return s[:11] or "Praxis"


@dataclass
class Result:
    ok: bool
    ref: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- HTTP (in Tests ersetzbar)
async def http_post_json(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers or {}) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = {}
            return resp.status, data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- Anbieter
class AspSms:
    name = "aspsms"

    def __init__(self, userkey: str, password: str):
        self.userkey, self.password = userkey, password

    @property
    def configured(self) -> bool:
        return bool(self.userkey and self.password)

    async def send(self, sender: str, to: str, text: str) -> Result:
        status, data = await http_post_json(ASPSMS_URL, {
            "UserName": self.userkey, "Password": self.password, "Originator": sender,
            "Recipients": [to], "MessageText": text, "ForceGSM7bit": False})
        if status == 200 and str(data.get("StatusCode")) == "1":
            return Result(True, ref=None)
        return Result(False, error=f"ASPSMS {status}: {data.get('StatusCode')} {data.get('StatusInfo') or ''}".strip())


class ECall:
    name = "ecall"

    def __init__(self, username: str, password: str):
        self.username, self.password = username, password

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)

    async def send(self, sender: str, to: str, text: str) -> Result:
        auth = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        status, data = await http_post_json(ECALL_URL, {
            "channel": "Sms", "from": sender, "to": to, "content": {"type": "Text", "text": text}},
            {"Authorization": f"Basic {auth}"})
        if status == 200 and data.get("messageId"):
            return Result(True, ref=str(data["messageId"]))
        return Result(False, error=f"eCall {status}: {data.get('errorCode') or ''} {data.get('errorMessage') or ''}".strip())


# --------------------------------------------------------------------------- Dienst
LEASE_MINUTES = 10


def claim(db: Database, msg_id: int) -> bool:
    """Nachricht im Postausgang für genau einen Versand reservieren. Ohne das schickte die Wiederholungsschleife eine frische
    Nachricht ein zweites Mal, wenn sie aufwachte, während der Sofortversand noch auf den Anbieter wartete.
    Stürzt der Server mitten im Versand ab, ist die Reservierung nach LEASE_MINUTES wieder frei."""
    lease = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES)).replace(microsecond=0).isoformat()
    return db.changes("UPDATE outbox SET next_try_at = ? WHERE id = ? AND status = 'queued' AND (next_try_at IS NULL OR next_try_at <= ?)",
                      (lease, msg_id, utcnow())) == 1


class SmsService:
    def __init__(self, settings, db: Database):
        self.settings = settings
        self.db = db
        self.providers = {
            "aspsms": AspSms(settings.aspsms_userkey, settings.aspsms_password),
            "ecall": ECall(settings.ecall_username, settings.ecall_password),
        }
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None

    # Für die Einstellungsseite
    @property
    def live(self) -> bool:
        return self.settings.sms_engine == "live"

    def available(self) -> list[str]:
        return ["log"] + [k for k, p in self.providers.items() if p.configured]

    def provider_of(self, practice_id: int) -> str:
        row = self.db.one("SELECT sms_provider FROM practices WHERE id = ?", (practice_id,))
        name = row["sms_provider"] if row else "log"
        if not self.live or name not in self.providers or not self.providers[name].configured:
            return "log"
        return name

    def sender_of(self, practice_id: int) -> str:
        row = self.db.one("SELECT sms_sender, name FROM practices WHERE id = ?", (practice_id,))
        return clean_sender((row["sms_sender"] or row["name"]) if row else "")

    def send(self, practice_id: int, channel: str, recipient: str, body: str, *, secrets=()) -> int:
        """In den Postausgang legen und (falls live) sofort im Hintergrund versenden. Gibt die Postausgang-Nr. zurück.
        secrets: Codes/Tokens im Text – gespeichert wird nur die maskierte Fassung (Klartext nur bis zum Versand)."""
        provider = self.provider_of(practice_id)
        number = normalize_phone(recipient)
        landline = bool(number) and is_landline(number)
        status = "logged" if provider == "log" else ("queued" if number and not landline else "failed")
        full = body[:MAX_LEN]
        masked = mask_secrets(full, secrets)
        stored, secret_body = masked, None
        if masked != full:
            if status == "queued":
                secret_body = full                      # nur bis zum Versand
            elif status == "logged" and getattr(self.settings, "demo", False):
                stored = full                           # Demo: fiktiv, nichts wird versendet – Text bleibt lesbar
        msg_id = self.db.execute(
            "INSERT INTO outbox (practice_id, channel, recipient, body, body_secret, status, created_at, provider, error)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (practice_id, channel, number or recipient, stored, secret_body, status, utcnow(), provider,
             None if provider == "log" or (number and not landline) else
             ("Festnetznummer – SMS nicht möglich" if landline else "Ungültige Telefonnummer")),
        )
        if status == "logged":
            log.info("Postausgang #%s (%s an %s) – nicht versendet (Testmodus)", msg_id, channel, mask(recipient))
        elif status == "queued":
            self._spawn(msg_id)
        return msg_id

    def _spawn(self, msg_id: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # ohne laufende Schleife (z. B. Befehlszeile): die Wiederholungsschleife übernimmt
        task = loop.create_task(self.deliver(msg_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def deliver(self, msg_id: int) -> bool:
        row = self.db.one("SELECT * FROM outbox WHERE id = ?", (msg_id,))
        if row is None or row["status"] != "queued" or row["channel"] == "email":
            return False
        provider = self.providers.get(row["provider"] or "")
        if provider is None or not provider.configured or not self.live:
            self.db.execute("UPDATE outbox SET status = 'failed', error = ?, body_secret = NULL WHERE id = ?",
                            ("Kein SMS-Anbieter eingerichtet", msg_id))
            return False
        if not claim(self.db, msg_id):
            return False   # wird gerade schon versendet (Sofortversand und Wiederholungsschleife gleichzeitig) – nie doppelt
        attempts = row["attempts"] + 1
        try:
            result = await provider.send(self.sender_of(row["practice_id"]), row["recipient"], row["body_secret"] or row["body"])
        except Exception as exc:  # noqa: BLE001 - Netzwerkfehler → später wiederholen
            result = Result(False, error=f"{type(exc).__name__}: {exc}"[:300])
        if result.ok:
            self.db.execute("UPDATE outbox SET status = 'sent', sent_at = ?, attempts = ?, provider_ref = ?, error = NULL,"
                            " body_secret = NULL WHERE id = ?", (utcnow(), attempts, result.ref, msg_id))
            return True
        final = attempts >= MAX_ATTEMPTS
        next_try = None if final else (datetime.now(timezone.utc) + timedelta(minutes=RETRY_MINUTES[attempts - 1])).replace(
            microsecond=0).isoformat()
        self.db.execute("UPDATE outbox SET status = ?, attempts = ?, error = ?, next_try_at = ?,"
                        " body_secret = CASE WHEN ? THEN NULL ELSE body_secret END WHERE id = ?",
                        ("failed" if final else "queued", attempts, (result.error or "")[:300], next_try, 1 if final else 0, msg_id))
        log.warning("SMS #%s an %s nicht versendet (Versuch %s): %s", msg_id, mask(row["recipient"]), attempts, result.error)
        return False

    def due_retries(self) -> list[int]:
        return [r["id"] for r in self.db.query(
            "SELECT id FROM outbox WHERE status = 'queued' AND channel != 'email' AND (next_try_at IS NULL OR next_try_at <= ?)"
            " ORDER BY id", (utcnow(),))]

    async def _retry_loop(self) -> None:
        while True:
            try:
                for msg_id in self.due_retries():
                    await self.deliver(msg_id)
            except Exception:  # pragma: no cover
                log.exception("SMS-Wiederholung fehlgeschlagen")
            await asyncio.sleep(60)

    async def start(self, app) -> None:
        if self.live:
            self._loop_task = asyncio.create_task(self._retry_loop())

    async def stop(self, app) -> None:
        for task in [self._loop_task, *self._tasks]:
            if task:
                task.cancel()


def recent(db: Database, practice_id: int, limit: int = 30):
    return db.query("SELECT id, channel, recipient, status, provider, attempts, error, created_at, sent_at FROM outbox"
                    " WHERE practice_id = ? ORDER BY id DESC LIMIT ?", (practice_id, limit))


def set_provider(db: Database, practice_id: int, provider: str, sender: str | None) -> None:
    if provider not in PROVIDERS:
        raise ValueError(provider)
    db.execute("UPDATE practices SET sms_provider = ?, sms_sender = ? WHERE id = ?",
               (provider, clean_sender(sender) if sender else None, practice_id))


def make_sms(settings, db: Database) -> SmsService:
    return SmsService(settings, db)


# Rückwärtskompatibel (ältere Importe)
OutboxSender = SmsService
_mask = mask
