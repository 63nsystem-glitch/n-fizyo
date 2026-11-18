"""E-Mail an Patient/innen über den Postausgang (Tabelle outbox, Kanal «email»).

- NFIZYO_MAIL=log (Standard, Demo, Tests): nichts verlässt die Box – die E-Mail steht nur im Postausgang (ohne Anhang).
- NFIZYO_MAIL=live: Versand über den SMTP-Server der Praxis/NSystem (NFIZYO_SMTP_HOST, …, nur in der .env-Datei).

E-Mail ist nicht verschlüsselt. Deshalb: nur an Patient/innen mit ausdrücklichem Einverständnis (patients.email_ok_at),
nur kurze Texte (Termin, Erinnerung) und auf Wunsch die Rechnung/Quittung als PDF. Berichte an Ärzt/innen gehen über HIN.
Anhänge werden nach dem Versand (oder sofort im Testmodus) aus dem Postausgang gelöscht. Absender: NFIZYO_SMTP_FROM mit
dem Namen der Praxis; Antworten gehen an die E-Mail-Adresse der Praxis (Reply-To).
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .db import Database, utcnow
from .sms import claim

log = logging.getLogger(__name__)

RETRY_MINUTES = (1, 5, 15, 60)
MAX_ATTEMPTS = len(RETRY_MINUTES) + 1
MAX_ATTACHMENT = 5 * 1024 * 1024
_EMAIL_RX = re.compile(r"^[^@\s<>,;\"]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}$")


def valid_email(value: str | None) -> bool:
    return bool(value and _EMAIL_RX.match(value.strip()))


def mask(addr: str) -> str:
    local, _, domain = addr.partition("@")
    return (local[:2] + "…@" + domain) if domain else "…"


class MailService:
    def __init__(self, settings, db: Database):
        self.settings = settings
        self.db = db
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.smtp_host and s.smtp_from and valid_email(s.smtp_from))

    @property
    def live(self) -> bool:
        return self.settings.mail_engine == "live" and self.configured

    def send(self, practice_id: int, recipient: str, subject: str, body: str,
             attachment: tuple[str, bytes] | None = None) -> int:
        """In den Postausgang legen und (falls live) sofort im Hintergrund versenden."""
        ok = valid_email(recipient)
        subject = " ".join(subject.split())  # keine Zeilenumbrüche im Betreff (Header)
        status = ("logged" if not self.live else "queued") if ok else "failed"
        name, data = attachment if attachment else (None, None)
        if data is not None and len(data) > MAX_ATTACHMENT:
            status = "failed"
        msg_id = self.db.execute(
            "INSERT INTO outbox (practice_id, channel, recipient, body, subject, attachment_name, attachment, status, created_at, provider,"
            " error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (practice_id, "email", recipient.strip(), body[:5000], subject[:200], name, data if status == "queued" else None, status,
             utcnow(), "smtp" if self.live else "log",
             None if status != "failed" else ("Ungültige E-Mail-Adresse" if not ok else "Anhang zu gross")))
        if status == "logged":
            log.info("Postausgang #%s (E-Mail an %s) – nicht versendet (Testmodus)", msg_id, mask(recipient))
        elif status == "queued":
            self._spawn(msg_id)
        return msg_id

    def _spawn(self, msg_id: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.deliver(msg_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _build(self, row) -> EmailMessage:
        practice = self.db.one("SELECT name, email FROM practices WHERE id = ?", (row["practice_id"],))
        msg = EmailMessage()
        msg["From"] = formataddr(((practice["name"] if practice else "Praxis")[:70], self.settings.smtp_from))
        msg["To"] = row["recipient"]
        msg["Subject"] = row["subject"] or ""
        msg["Message-ID"] = make_msgid(domain=self.settings.smtp_from.split("@")[-1])
        if practice and valid_email(practice["email"]):
            msg["Reply-To"] = practice["email"]
        msg.set_content(row["body"])
        if row["attachment"] is not None and row["attachment_name"]:
            msg.add_attachment(bytes(row["attachment"]), maintype="application", subtype="pdf", filename=row["attachment_name"])
        return msg

    def _smtp_send(self, msg: EmailMessage) -> None:
        s = self.settings
        ctx = ssl.create_default_context()
        if s.smtp_tls == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        try:
            if s.smtp_tls == "starttls":
                server.starttls(context=ctx)
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass

    async def deliver(self, msg_id: int) -> bool:
        row = self.db.one("SELECT * FROM outbox WHERE id = ? AND channel = 'email'", (msg_id,))
        if row is None or row["status"] != "queued":
            return False
        if not self.live:
            self.db.execute("UPDATE outbox SET status = 'failed', error = ?, attachment = NULL WHERE id = ?", ("Kein E-Mail-Versand eingerichtet", msg_id))
            return False
        if not claim(self.db, msg_id):
            return False   # wird gerade schon versendet (Sofortversand und Wiederholungsschleife gleichzeitig) – nie doppelt
        attempts = row["attempts"] + 1
        try:
            await asyncio.to_thread(self._smtp_send, self._build(row))
            error = None
        except Exception as exc:  # noqa: BLE001 - Netzwerk/Server → später wiederholen
            error = f"{type(exc).__name__}: {exc}"[:300]
        if error is None:
            self.db.execute("UPDATE outbox SET status = 'sent', sent_at = ?, attempts = ?, error = NULL, attachment = NULL WHERE id = ?",
                            (utcnow(), attempts, msg_id))
            return True
        final = attempts >= MAX_ATTEMPTS
        next_try = None if final else (datetime.now(timezone.utc) + timedelta(minutes=RETRY_MINUTES[attempts - 1])).replace(
            microsecond=0).isoformat()
        self.db.execute("UPDATE outbox SET status = ?, attempts = ?, error = ?, next_try_at = ?, attachment = CASE WHEN ? THEN NULL"
                        " ELSE attachment END WHERE id = ?", ("failed" if final else "queued", attempts, error, next_try, final, msg_id))
        log.warning("E-Mail #%s an %s nicht versendet (Versuch %s): %s", msg_id, mask(row["recipient"]), attempts, error)
        return False

    def due_retries(self) -> list[int]:
        return [r["id"] for r in self.db.query(
            "SELECT id FROM outbox WHERE status = 'queued' AND channel = 'email' AND (next_try_at IS NULL OR next_try_at <= ?)"
            " ORDER BY id", (utcnow(),))]

    async def _retry_loop(self) -> None:
        while True:
            try:
                for msg_id in self.due_retries():
                    await self.deliver(msg_id)
            except Exception:  # pragma: no cover
                log.exception("E-Mail-Wiederholung fehlgeschlagen")
            await asyncio.sleep(60)

    async def start(self, app) -> None:
        if self.live:
            self._loop_task = asyncio.create_task(self._retry_loop())

    async def stop(self, app) -> None:
        for task in [self._loop_task, *self._tasks]:
            if task:
                task.cancel()
